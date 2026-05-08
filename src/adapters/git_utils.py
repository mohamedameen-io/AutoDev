"""Git utility helpers shared across platform adapters."""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "_git_porcelain_set",
    "_diff_files",
    "_git_diff",
    "_git_diff_range",
    "_git_rev_parse_head",
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
