"""v0.24.3: enforce filesystem existence of architect-emitted file paths.

Released to close the bug class where the architect LLM emits a markdown
plan whose ``Files:`` lines reference paths that don't exist on disk —
previously the worker died mid-task with ``[Errno 63] File name too long``
because the path was a real directory prefix grafted onto literal source
content (``Pp.cpp`` and friends from a vendored 358K-file subtree).

The flow:

1. :func:`orchestrator.plan_parser.parse_plan_markdown` returns a structurally
   valid :class:`Plan` (text-only validation; no filesystem coupling).
2. :func:`validate_files_exist` walks every ``Task.files``,
   ``Task.extended_scope``, ``Phase.edit_scope``, and ``Plan.edit_scope``
   entry and confirms the path either:

   * exists as a tracked file in the repo (for ``Task.files``), OR
   * is a directory prefix under which at least one tracked file lives
     (for the ``*_scope`` lists), OR
   * is listed in ``Task.files_new`` — the v0.24.3 opt-out for paths the
     task itself will create.

3. On the first miss, :class:`PathValidationError` flows back into the
   existing v0.22.4 architect-retry envelope at
   :mod:`orchestrator.plan_phase` (lines 173-211). The caught exception
   carries ``raw`` (the offending path), ``reason="missing_on_disk"``,
   and ``suggestion`` (a difflib fuzzy match against the cached
   ``git ls-files`` snapshot), so the architect can self-correct on the
   second pass.

Why a separate module: keeps :mod:`plan_parser` pure-text (existing tests
stay green; ``parse_plan_markdown`` remains usable in fixtures without a
real filesystem) and keeps :mod:`path_validator` focused on string-shape
validation per its own docstring scope.
"""

from __future__ import annotations

import difflib
import subprocess
from pathlib import Path

from orchestrator.path_validator import PathValidationError
from state.schemas import Plan


class _RepoFileSnapshot:
    """Lazy, one-shot ``git ls-files`` cache.

    Used both for existence checks (``exists`` / ``is_dir_prefix``) and
    fuzzy-match suggestion (``closest``). The snapshot is built on first
    lookup and cached on the instance for the duration of one
    :func:`validate_files_exist` call — this keeps the subprocess cost
    bounded at one call per validation pass, not one per missing file.

    Subprocess invocation mirrors the pattern in
    :mod:`runtime.repo_probe` (``["git", "ls-files"]`` with
    ``capture_output=True``). On non-git trees or git failure the snapshot
    becomes the empty set; ``exists`` then returns ``False`` for everything
    and the caller decides whether to fail-soft or hard.
    """

    __slots__ = ("_cwd", "_tracked", "_loaded")

    def __init__(self, cwd: Path) -> None:
        self._cwd = cwd
        self._tracked: frozenset[str] = frozenset()
        self._loaded = False

    @classmethod
    def for_cwd(cls, cwd: Path) -> "_RepoFileSnapshot":
        """Build a snapshot bound to *cwd*. Subprocess runs lazily on first lookup."""
        return cls(cwd)

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            out = subprocess.run(
                ["git", "ls-files"],
                cwd=str(self._cwd),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return
        if out.returncode != 0:
            return
        self._tracked = frozenset(
            line.strip() for line in out.stdout.splitlines() if line.strip()
        )

    @property
    def is_empty(self) -> bool:
        """True when the snapshot has no tracked files.

        Distinguishes "no validation possible" (non-git tree, git failure,
        or empty repo) from "snapshot loaded but path absent". The
        validator short-circuits on an empty snapshot — without ground
        truth we have no basis to flag any path as missing.
        """
        self._ensure_loaded()
        return len(self._tracked) == 0

    def exists(self, rel_path: str) -> bool:
        """Set membership against the tracked-file snapshot."""
        self._ensure_loaded()
        return rel_path in self._tracked

    def is_dir_prefix(self, rel_path: str) -> bool:
        """True if any tracked file path starts with ``rel_path + "/"``.

        Used for ``*_scope`` list entries which are repo-relative directory
        prefixes (not files). An empty / no-op scope is the caller's
        responsibility to short-circuit before calling here — we treat the
        empty string as "no prefix to check" and return ``False``.
        """
        if not rel_path:
            return False
        self._ensure_loaded()
        prefix = rel_path.rstrip("/") + "/"
        return any(p.startswith(prefix) for p in self._tracked)

    def closest(self, rel_path: str) -> str | None:
        """Return the closest tracked path via :func:`difflib.get_close_matches`.

        ``cutoff=0.7`` filters out hopelessly-different paths (returns
        ``None`` rather than a misleading suggestion). ``n=1`` keeps the
        suggestion surface single-shot; the v0.25.0 index swap will likely
        return higher-quality matches but the v0.24.3 path is sufficient
        for the bug class we're closing today.
        """
        self._ensure_loaded()
        if not self._tracked:
            return None
        matches = difflib.get_close_matches(
            rel_path, self._tracked, n=1, cutoff=0.7
        )
        return matches[0] if matches else None


def validate_files_exist(plan: Plan, cwd: Path) -> None:
    """Raise :class:`PathValidationError` on the first missing path.

    Walks, in order:

    * Each ``Task.files`` entry (existence as a tracked file).
    * Each ``Task.extended_scope`` entry (directory prefix — at least one
      tracked file under ``cwd/<prefix>/``).
    * Each ``Phase.edit_scope`` entry (same dir-prefix semantics).
    * ``Plan.edit_scope`` entries (same).

    Paths listed in ``Task.files_new`` are skipped during the
    ``Task.files`` walk — they are the architect's declared
    about-to-be-created files and have no on-disk presence yet.

    On the first miss::

        raise PathValidationError(
            raw=offending_path,
            reason="missing_on_disk",
            suggestion=snapshot.closest(offending_path),
        )

    The caller (``orchestrator.plan_phase.run_plan_phase``) catches this
    exception in the existing v0.22.4 retry envelope and feeds the
    structured error back to the architect for a corrected second pass.

    No-op short-circuit: when ``cwd`` is not a git tree (or git ls-files
    yields nothing), the snapshot is empty and we have no basis to flag
    paths as missing. We skip silently in that case — non-git contexts
    (test fixtures, scratch dirs, brand-new ``git init`` with no files)
    aren't the population this validator was built for.
    """
    snapshot = _RepoFileSnapshot.for_cwd(cwd)
    if snapshot.is_empty:
        return

    # Plan-level edit_scope: dir-prefix check.
    for entry in plan.edit_scope:
        if not snapshot.is_dir_prefix(entry):
            raise PathValidationError(
                raw=entry,
                reason="missing_on_disk",
                suggestion=snapshot.closest(entry),
            )

    for phase in plan.phases:
        # Phase-level edit_scope override (None = inherit, list = explicit).
        if phase.edit_scope is not None:
            for entry in phase.edit_scope:
                if not snapshot.is_dir_prefix(entry):
                    raise PathValidationError(
                        raw=entry,
                        reason="missing_on_disk",
                        suggestion=snapshot.closest(entry),
                    )

        for task in phase.tasks:
            new_files: frozenset[str] = frozenset(task.files_new or [])

            for entry in task.files:
                if entry in new_files:
                    continue
                if not snapshot.exists(entry):
                    raise PathValidationError(
                        raw=entry,
                        reason="missing_on_disk",
                        suggestion=snapshot.closest(entry),
                    )

            for entry in task.extended_scope:
                if not snapshot.is_dir_prefix(entry):
                    raise PathValidationError(
                        raw=entry,
                        reason="missing_on_disk",
                        suggestion=snapshot.closest(entry),
                    )


__all__ = ["validate_files_exist"]
