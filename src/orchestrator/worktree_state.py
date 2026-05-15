"""Worktree state manifest at ``.autodev/worktrees-state.json``.

v0.31.0 (Phase 5.2): the orchestrator records every git worktree it
creates so post-crash cleanup can reconcile what's on disk against what
the orchestrator believed was alive. This is the data source for:

* ``autodev prune --executor-only --all`` — sweep orphans after SIGKILL.
* ``autodev doctor --repair-worktrees`` — list orphans without deleting.
* future resume logic that needs to recover in-flight worktree state.

Layout::

    {
      "version": 1,
      "entries": [
        {
          "path": "/abs/path/to/.autodev/execute_worktrees/tasks/1.1",
          "label": "1.1",
          "task_id": "1.1",
          "created_at": "2026-05-15T10:30:00Z",
          "pid_of_creator": 41523
        },
        ...
      ]
    }

Writes go through :func:`_atomic_write` (write to ``.tmp`` + ``os.replace``)
so a partial-failure mid-write never leaves the manifest corrupt.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


MANIFEST_FILENAME = "worktrees-state.json"
MANIFEST_VERSION = 1


@dataclass
class WorktreeEntry:
    """One live worktree as recorded by :class:`WorktreeManager`."""

    path: str
    label: str
    task_id: str | None
    created_at: str
    pid_of_creator: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _manifest_path(autodev_root: Path) -> Path:
    return autodev_root / MANIFEST_FILENAME


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` to ``path`` atomically.

    Strategy: write to ``<path>.tmp`` then ``os.replace`` over the
    destination. ``os.replace`` is atomic on POSIX and Windows, so a
    crash mid-write either leaves the prior file intact OR the new file
    fully written -- never a half-written file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(payload, indent=2, sort_keys=True)
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load_manifest(autodev_root: Path) -> list[WorktreeEntry]:
    """Read entries from the manifest. Returns ``[]`` if missing/invalid.

    Defensive on every failure mode (missing file, bad JSON, schema
    mismatch) -- the manifest is best-effort observability, not a hard
    contract. Callers that need precise counts should treat ``[]`` as
    "manifest unavailable, fall back to disk-walk".
    """
    p = _manifest_path(autodev_root)
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, dict):
        return []
    entries = raw.get("entries", [])
    if not isinstance(entries, list):
        return []
    out: list[WorktreeEntry] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        try:
            out.append(
                WorktreeEntry(
                    path=str(item["path"]),
                    label=str(item.get("label", "")),
                    task_id=(
                        str(item["task_id"])
                        if item.get("task_id") is not None
                        else None
                    ),
                    created_at=str(item.get("created_at", "")),
                    pid_of_creator=int(item.get("pid_of_creator", 0)),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _save_entries(autodev_root: Path, entries: list[WorktreeEntry]) -> None:
    payload = {
        "version": MANIFEST_VERSION,
        "entries": [e.to_dict() for e in entries],
    }
    _atomic_write(_manifest_path(autodev_root), payload)


def record_create(
    autodev_root: Path,
    *,
    path: Path,
    label: str,
    task_id: str | None,
) -> None:
    """Append a new entry to the manifest after a successful create.

    Idempotent on duplicate ``path`` -- the existing entry is replaced
    rather than duplicated. Best-effort: any I/O failure is swallowed so
    a manifest hiccup never breaks worktree creation.
    """
    try:
        entries = load_manifest(autodev_root)
        abs_path = str(path.resolve()) if path.exists() else str(path)
        entries = [e for e in entries if e.path != abs_path]
        entries.append(
            WorktreeEntry(
                path=abs_path,
                label=label,
                task_id=task_id,
                created_at=time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                ),
                pid_of_creator=os.getpid(),
            )
        )
        _save_entries(autodev_root, entries)
    except OSError:
        pass


def record_cleanup(autodev_root: Path, *, path: Path) -> None:
    """Remove the entry for ``path`` after a successful cleanup.

    Best-effort: any I/O failure is swallowed so a manifest hiccup never
    breaks the cleanup happy path.
    """
    try:
        entries = load_manifest(autodev_root)
        candidates = {str(path), str(path.resolve()) if path.exists() else str(path)}
        entries = [e for e in entries if e.path not in candidates]
        _save_entries(autodev_root, entries)
    except OSError:
        pass


def find_orphans(
    autodev_root: Path, *, scan_dirs: list[Path] | None = None
) -> dict[str, list[str]]:
    """Return orphan paths in two categories.

    Returns::

        {
          "manifest_missing_on_disk": [path, ...],
          "on_disk_not_in_manifest": [path, ...],
        }

    * ``manifest_missing_on_disk`` -- the orchestrator believed these
      worktrees existed but their on-disk directory is gone. Usually
      from a crash mid-cleanup; the manifest entry can be safely
      pruned.
    * ``on_disk_not_in_manifest`` -- worktree-shaped directories under
      ``scan_dirs`` that have no manifest entry. Usually from a crash
      mid-create OR from an older AutoDev that pre-dates the manifest.
      ``scan_dirs`` defaults to the conventional executor worktree
      roots.
    """
    if scan_dirs is None:
        scan_dirs = [
            autodev_root / "execute_worktrees",
            autodev_root / "execute_worktrees_pool",
        ]

    entries = load_manifest(autodev_root)

    manifest_missing: list[str] = []
    for e in entries:
        if not Path(e.path).exists():
            manifest_missing.append(e.path)

    known_paths: set[str] = set()
    for e in entries:
        known_paths.add(e.path)
        try:
            known_paths.add(str(Path(e.path).resolve()))
        except OSError:
            pass

    on_disk: list[str] = []
    for root in scan_dirs:
        if not root.exists():
            continue
        for child in root.iterdir():
            if not child.is_dir():
                continue
            # Accept either the immediate child OR (for tasks/) its
            # grandchildren as worktree-shaped.
            candidates: list[Path] = [child]
            if child.name == "tasks":
                for gc in child.iterdir():
                    if gc.is_dir():
                        candidates.append(gc)
            for cand in candidates:
                cand_paths = {str(cand)}
                try:
                    cand_paths.add(str(cand.resolve()))
                except OSError:
                    pass
                if not (cand_paths & known_paths):
                    on_disk.append(str(cand))

    return {
        "manifest_missing_on_disk": manifest_missing,
        "on_disk_not_in_manifest": on_disk,
    }


__all__ = [
    "MANIFEST_FILENAME",
    "MANIFEST_VERSION",
    "WorktreeEntry",
    "find_orphans",
    "load_manifest",
    "record_cleanup",
    "record_create",
]
