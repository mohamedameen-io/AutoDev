"""``autodev prune`` — garbage-collect stale per-run artifacts.

Walks three directories and removes children whose mtime is older than
``--older-than``:

* ``tournaments/<tournament_id>/`` — each tournament's full subtree
* ``sessions/<session_id>/`` — each session's full subtree
* ``evidence/`` — individual files (``<task_id>-{kind}.json``,
  ``<task_id>.patch``) accumulate per task across runs

Always preserved: ``plan.json``, ``plan-ledger.jsonl``,
``knowledge.jsonl``, ``rejected_lessons.jsonl``, ``config.json``,
``spec.md``, the file index, and ``secretscan-baseline.json``. Use
``autodev reset`` to clear plan state.
"""

from __future__ import annotations

import re
import shutil
import sys
import time
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from state.paths import (
    autodev_root,
    evidence_dir,
    sessions_dir,
    tournaments_dir,
)


_DURATION_RE = re.compile(r"^(\d+)([smhd])$")
_UNIT_SECONDS: dict[str, float] = {
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
}


def _parse_duration(text: str) -> float:
    """Parse ``Ns``/``Nm``/``Nh``/``Nd`` into seconds.

    Raises :class:`ValueError` on any other shape. Empty, negative,
    fractional, and unit-less values are rejected.
    """
    if not isinstance(text, str):
        raise ValueError(f"duration must be a string, got {type(text).__name__}")
    m = _DURATION_RE.match(text)
    if not m:
        raise ValueError(
            f"invalid duration {text!r}; expected e.g. 30d, 24h, 60m, 30s"
        )
    qty, unit = int(m.group(1)), m.group(2)
    if qty == 0:
        raise ValueError(f"invalid duration {text!r}; must be non-zero")
    return qty * _UNIT_SECONDS[unit]


def _children_older_than(parent: Path, threshold_s: float) -> list[Path]:
    """Return immediate children of ``parent`` whose mtime is older than
    ``threshold_s`` seconds ago. Missing parent returns empty list."""
    if not parent.exists():
        return []
    cutoff = time.time() - threshold_s
    stale: list[Path] = []
    for child in parent.iterdir():
        try:
            if child.stat().st_mtime < cutoff:
                stale.append(child)
        except OSError:
            continue
    return stale


def _remove(path: Path) -> None:
    """Remove a file or directory; swallow OSError so prune is best-effort."""
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink()
    except OSError:
        pass


@click.command("prune")
@click.option(
    "--older-than",
    default="30d",
    help="Age threshold (e.g. 30d, 7d, 24h, 60m, 30s).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="List what would be removed without deleting anything.",
)
def prune(older_than: str, dry_run: bool) -> None:
    """Garbage-collect stale tournament, session, and evidence artifacts."""
    console = Console()
    try:
        threshold_s = _parse_duration(older_than)
    except ValueError as exc:
        console.print(f"[red]autodev prune:[/red] {exc}")
        sys.exit(1)

    cwd = Path.cwd()
    if not autodev_root(cwd).exists():
        console.print(
            "[yellow]autodev prune:[/yellow] .autodev/ not found — "
            "nothing to prune."
        )
        sys.exit(0)

    stale: list[tuple[Path, str]] = []
    for parent, label in (
        (tournaments_dir(cwd), "tournament"),
        (sessions_dir(cwd), "session"),
        (evidence_dir(cwd), "evidence"),
    ):
        for child in _children_older_than(parent, threshold_s):
            stale.append((child, label))

    if not stale:
        console.print(
            f"[green]autodev prune:[/green] nothing older than "
            f"{older_than} on disk."
        )
        sys.exit(0)

    table = Table(
        title=("Would remove" if dry_run else "Removed"),
        show_header=True,
        header_style=("bold yellow" if dry_run else "bold red"),
    )
    table.add_column("Path", overflow="fold")
    table.add_column("Kind", style="dim")
    for path, label in stale:
        try:
            rel = path.relative_to(cwd)
        except ValueError:
            rel = path
        table.add_row(str(rel), label)
        if not dry_run:
            _remove(path)
    console.print(table)
    verb = "would remove" if dry_run else "removed"
    console.print(
        f"[green]autodev prune:[/green] {verb} {len(stale)} path(s) "
        f"older than {older_than}."
    )
