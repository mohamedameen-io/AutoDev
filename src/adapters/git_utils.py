"""Git utility helpers shared across platform adapters."""

from __future__ import annotations

from pathlib import Path

__all__ = ["_git_porcelain_set", "_diff_files", "_git_diff"]


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
