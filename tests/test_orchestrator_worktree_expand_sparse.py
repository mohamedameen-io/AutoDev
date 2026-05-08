"""v0.20.0 C3: WorktreeManager.expand_sparse_paths tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orchestrator.worktree import (
    WorktreeError,
    WorktreeManager,
    detect_missing_paths,
)


# ---------------------------------------------------------------------------
# detect_missing_paths
# ---------------------------------------------------------------------------


def test_detect_missing_paths_bash_message() -> None:
    txt = "bash: src/foo/bar.py: No such file or directory"
    assert "src/foo/bar.py" in detect_missing_paths(txt)


def test_detect_missing_paths_python_message() -> None:
    txt = "FileNotFoundError: [Errno 2] No such file or directory: 'src/qa/x.py'"
    assert "src/qa/x.py" in detect_missing_paths(txt)


def test_detect_missing_paths_dedupes() -> None:
    txt = (
        "src/x.py: No such file or directory\n"
        "FileNotFoundError: ... 'src/x.py'\n"
    )
    paths = detect_missing_paths(txt)
    assert paths.count("src/x.py") == 1


def test_detect_missing_paths_filters_absolute_paths() -> None:
    txt = "/etc/passwd: No such file or directory"
    assert "/etc/passwd" not in detect_missing_paths(txt)


def test_detect_missing_paths_filters_parent_segments() -> None:
    txt = "../escape.txt: No such file or directory"
    assert "../escape.txt" not in detect_missing_paths(txt)


def test_detect_missing_paths_empty_text_returns_empty() -> None:
    assert detect_missing_paths("") == []
    assert detect_missing_paths(None) == []  # type: ignore[arg-type]


def test_detect_missing_paths_handles_no_match() -> None:
    assert detect_missing_paths("Everything is fine.") == []


# ---------------------------------------------------------------------------
# expand_sparse_paths — uses real git on tmp repo
# ---------------------------------------------------------------------------


def _git(cmd: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git"] + cmd,
        cwd=str(cwd),
        check=True,
        capture_output=True,
    )


def _make_repo(tmp: Path) -> Path:
    repo = tmp / "main"
    repo.mkdir()
    _git(["init"], repo)
    _git(["config", "user.email", "t@t"], repo)
    _git(["config", "user.name", "tester"], repo)
    # Two top-level dirs
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("# a\n")
    (repo / "extra").mkdir()
    (repo / "extra" / "x.py").write_text("# x\n")
    _git(["add", "."], repo)
    _git(["commit", "-m", "init"], repo)
    return repo


@pytest.mark.asyncio
async def test_expand_sparse_paths_idempotent_on_empty(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    mgr = WorktreeManager(repo, tmp_path / "tournaments")
    # Empty additional_paths → no-op
    await mgr.expand_sparse_paths(tmp_path / "fake", [])  # missing OK on empty


@pytest.mark.asyncio
async def test_expand_sparse_paths_widens_existing_sparse_worktree(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    mgr = WorktreeManager(repo, tmp_path / "tournaments")
    wt = await mgr.create("a", sparse_paths=["src"])
    # extra/ NOT materialized
    assert (wt / "src" / "a.py").exists()
    assert not (wt / "extra" / "x.py").exists()
    # Widen
    await mgr.expand_sparse_paths(wt, ["extra"])
    assert (wt / "extra" / "x.py").exists()


@pytest.mark.asyncio
async def test_expand_sparse_paths_idempotent_on_existing_path(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    mgr = WorktreeManager(repo, tmp_path / "tournaments")
    wt = await mgr.create("a", sparse_paths=["src", "extra"])
    # Add same prefix again — must not error.
    await mgr.expand_sparse_paths(wt, ["extra"])
    assert (wt / "extra" / "x.py").exists()


@pytest.mark.asyncio
async def test_expand_sparse_paths_raises_on_missing_worktree(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    mgr = WorktreeManager(repo, tmp_path / "tournaments")
    fake = tmp_path / "does-not-exist"
    with pytest.raises(WorktreeError):
        await mgr.expand_sparse_paths(fake, ["src"])
