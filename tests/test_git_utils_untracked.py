"""v0.22.1 A5 regression: ``_git_diff_with_untracked`` captures new files.

The legacy ``_git_diff`` calls ``git diff HEAD`` only, which omits
untracked files. Every developer task that created new files (e.g.
``notes/foo.md``) had ``evidence.diff = null`` despite
``files_changed`` being populated — surfaced by D-3 in the 2026-05-09
Unity stall investigation. The sibling helper iterates untracked files
and splices per-file ``--no-index`` diff blocks.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from adapters.git_utils import _git_diff, _git_diff_with_untracked


def _init_git(p: Path) -> None:
    subprocess.run(["git", "init", str(p)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(p), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(p), check=True)
    (p / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "seed.txt"], cwd=str(p), check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(p),
        check=True,
        capture_output=True,
    )


def test_legacy_git_diff_misses_untracked(tmp_path: Path) -> None:
    """The pre-A5 helper returns empty/None for untracked-only changes.

    This documents the bug A5 fixes; if this assertion ever fails it
    means git's ``git diff HEAD`` started showing untracked files (it
    doesn't, but pin the invariant for clarity).
    """
    _init_git(tmp_path)
    (tmp_path / "new_file.py").write_text("print('hi')\n")
    diff = _git_diff(tmp_path)
    assert not diff


def test_with_untracked_captures_new_file(tmp_path: Path) -> None:
    _init_git(tmp_path)
    (tmp_path / "new_file.py").write_text("print('hi')\n")
    diff = _git_diff_with_untracked(tmp_path)
    assert diff is not None
    assert "new_file.py" in diff
    assert "print('hi')" in diff


def test_with_untracked_captures_mixed(tmp_path: Path) -> None:
    """Modifying a tracked file AND creating an untracked file: both surface."""
    _init_git(tmp_path)
    (tmp_path / "seed.txt").write_text("seed-modified\n")
    (tmp_path / "untracked.md").write_text("# new\n")
    diff = _git_diff_with_untracked(tmp_path)
    assert diff is not None
    assert "seed.txt" in diff
    assert "untracked.md" in diff
    assert "seed-modified" in diff
    assert "# new" in diff


def test_with_untracked_clean_repo_returns_none(tmp_path: Path) -> None:
    _init_git(tmp_path)
    assert _git_diff_with_untracked(tmp_path) is None


def test_with_untracked_non_repo_returns_none(tmp_path: Path) -> None:
    """Outside a git repo: graceful None like the legacy helper."""
    assert _git_diff_with_untracked(tmp_path) is None


def test_with_untracked_skips_gitignored(tmp_path: Path) -> None:
    """``--exclude-standard`` filters .gitignored files out of the listing."""
    _init_git(tmp_path)
    (tmp_path / ".gitignore").write_text("*.log\n")
    subprocess.run(
        ["git", "add", ".gitignore"], cwd=str(tmp_path), check=True
    )
    subprocess.run(
        ["git", "commit", "-m", "add gitignore"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    (tmp_path / "ignored.log").write_text("noise\n")
    (tmp_path / "kept.md").write_text("keep\n")
    diff = _git_diff_with_untracked(tmp_path)
    assert diff is not None
    assert "kept.md" in diff
    # The ignored.log line MUST NOT appear (it's gitignored).
    assert "ignored.log" not in diff


def test_with_untracked_excludes_autodev_state(tmp_path: Path) -> None:
    """WS2: AutoDev's own untracked ``.autodev/`` state must not leak here.

    A fresh target repo's ``.gitignore`` has never heard of AutoDev, so its
    own run-state (ledger, tournament artifacts, the language-profile
    cache, ...) shows up to ``git ls-files --others`` exactly like a real
    new file. The module-level ``_list_untracked`` does NOT itself filter
    it (unlike ``WorktreeManager._list_untracked``); the protection is the
    trailing ``filter_generated_from_diff`` pass in
    ``_git_diff_with_untracked``, which drops the ``.autodev/*`` section.
    The legitimate untracked source file must still survive.
    """
    _init_git(tmp_path)
    (tmp_path / "real_change.py").write_text("x = 1\n")
    autodev = tmp_path / ".autodev"
    autodev.mkdir()
    (autodev / "language_profile.json").write_text(
        '{"profile": {"python": 1.0}}\n'
    )
    diff = _git_diff_with_untracked(tmp_path)
    assert diff is not None
    assert "real_change.py" in diff
    assert "language_profile.json" not in diff
    assert ".autodev" not in diff
