"""Tests for src.adapters.git_utils."""

from __future__ import annotations

import subprocess
from pathlib import Path


from adapters.git_utils import _diff_files, _git_diff, _git_porcelain_set


def _init_git_repo(path: Path) -> None:
    """Initialise a minimal git repo at *path* suitable for testing."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# _git_porcelain_set
# ---------------------------------------------------------------------------


def test_git_porcelain_set_returns_none_when_not_git_repo(tmp_path: Path) -> None:
    result = _git_porcelain_set(tmp_path)
    assert result is None


def test_git_porcelain_set_returns_set_in_git_repo(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    # Create an untracked file so the set is non-empty.
    (tmp_path / "hello.txt").write_text("hi")
    result = _git_porcelain_set(tmp_path)
    assert isinstance(result, set)
    assert "hello.txt" in result


def test_git_porcelain_set_returns_empty_set_for_clean_repo(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    # Commit a file so the repo is clean.
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("content")
    subprocess.run(
        ["git", "add", "tracked.txt"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    result = _git_porcelain_set(tmp_path)
    assert result == set()


# ---------------------------------------------------------------------------
# _diff_files
# ---------------------------------------------------------------------------


def test_diff_files_returns_empty_when_sets_equal() -> None:
    s = {"a.py", "b.py"}
    assert _diff_files(s, s) == []


def test_diff_files_returns_new_files_when_sets_differ() -> None:
    before = {"a.py"}
    after = {"a.py", "b.py", "c.py"}
    assert _diff_files(before, after) == ["b.py", "c.py"]


def test_diff_files_returns_empty_when_before_is_none() -> None:
    assert _diff_files(None, {"a.py"}) == []


def test_diff_files_returns_empty_when_after_is_none() -> None:
    assert _diff_files({"a.py"}, None) == []


def test_diff_files_returns_empty_when_both_none() -> None:
    assert _diff_files(None, None) == []


# ---------------------------------------------------------------------------
# _git_diff
# ---------------------------------------------------------------------------


def test_git_diff_returns_none_when_not_git_repo(tmp_path: Path) -> None:
    result = _git_diff(tmp_path)
    assert result is None


def test_git_diff_returns_none_for_repo_with_no_commits(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    # No commits yet — `git diff HEAD` will fail (non-zero exit).
    result = _git_diff(tmp_path)
    assert result is None


def test_git_diff_returns_diff_string_when_files_changed(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    tracked = tmp_path / "file.txt"
    tracked.write_text("original\n")
    subprocess.run(
        ["git", "add", "file.txt"], cwd=str(tmp_path), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    # Modify the file so there is a diff.
    tracked.write_text("modified\n")
    result = _git_diff(tmp_path)
    assert result is not None
    assert "file.txt" in result


def test_git_diff_returns_none_for_clean_repo(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    tracked = tmp_path / "file.txt"
    tracked.write_text("original\n")
    subprocess.run(
        ["git", "add", "file.txt"], cwd=str(tmp_path), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    # No changes — diff should be empty string, which maps to None.
    result = _git_diff(tmp_path)
    assert result is None
