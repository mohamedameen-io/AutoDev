"""Git utility helpers shared across platform adapters."""

from __future__ import annotations

import logging
from pathlib import Path


_log = logging.getLogger(__name__)


# Bug #3 (v0.25.1): cap path length at POSIX ``NAME_MAX`` (255). Developer
# agents occasionally emit a JSON-escaped multi-line code listing into the
# ``diff`` field of ``.autodev/responses/{task_id}-{role}.json``; the entire
# 4000+ char blob then arrives as a single ``+++ b/<path>`` line. The
# downstream QA helpers' ``Path.is_file()`` then raises ``OSError [Errno 63]
# File name too long``. Sanitising at the source keeps the rest of the
# pipeline simple.
_MAX_PATH_LEN = 255

__all__ = [
    "_git_porcelain_set",
    "_diff_files",
    "_git_diff",
    "_git_diff_with_untracked",
    "_git_diff_range",
    "_git_rev_parse_head",
    "extract_files_from_diff",
]


def _git_porcelain_set(cwd: Path) -> set[str] | None:
    """Snapshot tracked+untracked filenames reported by `git status --porcelain`.

    Returns None if `cwd` is not a git repo (no `.git` dir), signalling that
    diff tracking is not possible.
    """
    try:
        cwd_path = Path(cwd)
    except TypeError:
        return None
    if not (cwd_path / ".git").exists():
        return None
    try:
        import subprocess  # local import to keep adapter core importable

        out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(cwd_path),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    files: set[str] = set()
    for line in out.stdout.splitlines():
        # porcelain format: "XY path" (first two cols are status flags).
        if len(line) < 4:
            continue
        path = line[3:].strip()
        # Handle rename entries "old -> new".
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        files.add(path)
    return files


def _diff_files(before: set[str] | None, after: set[str] | None) -> list[str]:
    """Return filenames that appear in `after` but not in `before`.

    `git status --porcelain` shows a line per changed-or-untracked file with a
    status prefix. If a tracked file is modified during the run, it will appear
    in `after` (with a modification flag) but not in `before` (if it was clean
    before). Newly-untracked files similarly only show in `after`. A file that
    was modified before AND is still modified after shows up in both sets with
    the same status line, so we'd miss it — acceptable for Phase 2 (we care
    about work the agent just did). Phase 3+ may switch to diff-based tracking.
    """
    if before is None or after is None:
        return []
    return sorted(after - before)


def _git_diff(cwd: Path) -> str | None:
    try:
        import subprocess

        out = subprocess.run(
            ["git", "diff", "HEAD"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout or None


def _list_untracked(cwd: Path) -> list[str]:
    """v0.22.1 A5: paths of untracked, non-gitignored files in *cwd*.

    Returns ``[]`` for non-repos / subprocess failure. Used by
    :func:`_git_diff_with_untracked` to surface new files in adapter
    evidence. Mirrors
    :meth:`orchestrator.worktree.WorktreeManager._list_untracked`.
    """
    try:
        import subprocess

        out = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    return [line for line in out.stdout.splitlines() if line.strip()]


def _git_diff_with_untracked(cwd: Path) -> str | None:
    """v0.22.1 A5: ``git diff HEAD`` plus a per-untracked-file diff block.

    The legacy :func:`_git_diff` calls ``git diff HEAD`` only — that
    omits untracked files. Every developer task that creates new files
    (e.g. ``notes/foo.md``) ended up with ``evidence.diff = null``
    despite ``files_changed`` being populated (D-3 finding from the
    2026-05-09 Unity stall). This sibling helper appends per-untracked
    ``git diff --no-color --no-index /dev/null <rel>`` blocks. ``git
    diff --no-index`` returns rc=1 when files differ (the success case
    here), so we accept rc in (0, 1). Returns ``None`` outside a git
    repo or when there is genuinely nothing to diff. Mirrors
    :meth:`orchestrator.worktree.WorktreeManager.get_diff_vs_base`.
    """
    import subprocess

    cwd_path = Path(cwd)
    if not (cwd_path / ".git").exists():
        return None
    # Tracked-side diff: empty string when clean (NOT the same as no-repo).
    base = _git_diff(cwd) or ""
    diff_text = base
    for rel in _list_untracked(cwd):
        try:
            out = subprocess.run(
                ["git", "diff", "--no-color", "--no-index", "/dev/null", rel],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode in (0, 1):
            diff_text += out.stdout
    return diff_text or None


def _git_diff_range(cwd: Path, from_sha: str, to_sha: str) -> str | None:
    """Return the unified diff between two commits, or ``None`` on failure.

    Used by the v0.9.0 phase-review tournament to materialize the
    "as-implemented" A variant of the :class:`PhaseReviewBundle` from the
    range ``phase.baseline_commit..HEAD``. Mirrors :func:`_git_diff` error
    handling (returns ``None`` on subprocess / git failure rather than
    raising) so the caller can degrade gracefully — phase review continues
    with an empty diff rather than blocking forward progress.

    The two-dot range form ``from_sha..to_sha`` is intentional: it shows
    every commit reachable from ``to_sha`` that isn't reachable from
    ``from_sha``. With a linear history this is the same as
    ``git diff from_sha to_sha`` but treats merges sensibly.
    """
    try:
        import subprocess

        out = subprocess.run(
            ["git", "diff", f"{from_sha}..{to_sha}"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout or None


def extract_files_from_diff(diff: str, *, strict: bool = False) -> list[str]:
    """Pull file paths from a unified diff (lightweight, deterministic).

    Parses the ``+++ b/<path>`` lines from a unified diff, returning paths
    in first-seen order with duplicates removed. ``+++ /dev/null`` (file
    deletion target) is naturally excluded because the prefix doesn't match.

    Lifted from ``orchestrator.phase_review_runner`` in v0.13.0 for reuse
    by the secretscan diff-scope path. The phase-review wrapper continues
    to import this function under the legacy private name to keep its
    callers stable.

    v0.27.0 (audit §6): adds a ``strict`` mode. With ``strict=False`` (the
    default — preserves v0.26.2 behaviour for legacy phase-review callers)
    a non-empty diff with no parseable ``+++ b/`` headers returns ``[]``
    silently. With ``strict=True`` the same condition raises
    :class:`errors.DiffParseError` so the QA-gate site can fail-closed on
    tasks declared as ``produces_diff=True`` rather than silently passing
    every diff-scoped gate with ``paths=[]``.

    Args:
        diff: Unified diff text. May be empty.
        strict: When ``True``, raise :class:`errors.DiffParseError` if the
            diff text is non-empty but no ``+++ b/`` header is parseable.

    Returns:
        Repo-relative paths for each file the diff modifies. Empty list
        when the diff is empty. With ``strict=False``, also empty when
        the diff contains no parseable headers.
    """
    if not diff:
        return []
    files: list[str] = []
    seen: set[str] = set()
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[len("+++ b/"):].strip()
            if not path or path in seen:
                continue
            # Bug #3 (v0.25.1): reject pathological paths. A 4000-char blob
            # with embedded newlines / NUL bytes is never a real path; the
            # downstream QA helpers' ``Path.is_file()`` raises
            # ``OSError [Errno 63] File name too long`` on it.
            reason: str | None = None
            if len(path) > _MAX_PATH_LEN:
                reason = f"len={len(path)} > {_MAX_PATH_LEN}"
            elif "\n" in path or "\\n" in path:
                reason = "embedded newline"
            elif "\x00" in path:
                reason = "embedded NUL"
            if reason is not None:
                _log.warning(
                    "extract_files_from_diff.rejected_path",
                    extra={
                        "reason": reason,
                        "path_prefix": path[:80],
                    },
                )
                continue
            files.append(path)
            seen.add(path)
    if not files and strict:
        from errors import DiffParseError

        raise DiffParseError(
            f"diff has {len(diff)} chars but no parseable '+++ b/' headers "
            f"(first 80 chars: {diff[:80]!r})"
        )
    return files


def _git_rev_parse_head(cwd: Path) -> str | None:
    """Return the current ``HEAD`` SHA, or ``None`` when not in a repo.

    Used at phase entry to record :attr:`Phase.baseline_commit` and again
    at phase completion to capture the tip commit for the phase-review
    diff. ``None`` is returned for the same scenarios as :func:`_git_diff`
    (no ``.git`` dir, subprocess failure, non-zero exit) so callers can
    skip phase review without raising.
    """
    try:
        cwd_path = Path(cwd)
    except TypeError:
        return None
    if not (cwd_path / ".git").exists():
        return None
    try:
        import subprocess

        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(cwd_path),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    sha = out.stdout.strip()
    return sha or None
