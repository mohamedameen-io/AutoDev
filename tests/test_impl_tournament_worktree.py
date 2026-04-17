"""Tests for :class:`WorktreeManager` using real git init + worktree operations."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orchestrator.worktree import WorktreeError, WorktreeManager


def _init_git_repo(path: Path) -> None:
    """Initialize a minimal git repo with one commit."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
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
    (path / "README.md").write_text("# test\n")
    subprocess.run(["git", "add", "."], cwd=str(path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )


@pytest.mark.asyncio
async def test_create_and_remove_worktree(tmp_path: Path) -> None:
    """Create a worktree, verify it exists, then remove it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    wt_dir = tmp_path / "worktrees"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)

    wt = await mgr.create("test-label", base_ref="HEAD")
    assert wt.exists()
    assert wt == wt_dir / "test-label"

    await mgr.remove("test-label")
    assert not wt.exists()


@pytest.mark.asyncio
async def test_create_duplicate_label_raises(tmp_path: Path) -> None:
    """Creating a worktree with an existing label raises WorktreeError."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    wt_dir = tmp_path / "worktrees"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)

    await mgr.create("dup", base_ref="HEAD")
    with pytest.raises(WorktreeError, match="already exists"):
        await mgr.create("dup", base_ref="HEAD")

    await mgr.cleanup_all()


@pytest.mark.asyncio
async def test_get_diff_vs_base_empty_for_clean_worktree(tmp_path: Path) -> None:
    """A freshly created worktree with no changes produces an empty diff."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    wt_dir = tmp_path / "worktrees"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)

    wt = await mgr.create("clean", base_ref="HEAD")
    diff = await mgr.get_diff_vs_base(wt)
    assert diff.strip() == ""

    await mgr.cleanup_all()


@pytest.mark.asyncio
async def test_get_diff_vs_base_captures_new_file(tmp_path: Path) -> None:
    """A new file added in the worktree appears in the diff."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    wt_dir = tmp_path / "worktrees"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)

    wt = await mgr.create("newfile", base_ref="HEAD")
    (wt / "new_module.py").write_text("def bar(): pass\n")

    diff = await mgr.get_diff_vs_base(wt)
    assert "new_module.py" in diff or "bar" in diff

    await mgr.cleanup_all()


@pytest.mark.asyncio
async def test_cleanup_all_removes_all_worktrees(tmp_path: Path) -> None:
    """cleanup_all removes all worktrees and the tournament dir."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    wt_dir = tmp_path / "worktrees"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)

    await mgr.create("a", base_ref="HEAD")
    await mgr.create("b", base_ref="HEAD")
    assert (wt_dir / "a").exists()
    assert (wt_dir / "b").exists()

    await mgr.cleanup_all()
    assert not (wt_dir / "a").exists()
    assert not (wt_dir / "b").exists()


@pytest.mark.asyncio
async def test_cleanup_all_idempotent(tmp_path: Path) -> None:
    """cleanup_all called twice does not raise."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    wt_dir = tmp_path / "worktrees"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)

    await mgr.create("x", base_ref="HEAD")
    await mgr.cleanup_all()
    await mgr.cleanup_all()  # second call must not raise


@pytest.mark.asyncio
async def test_remove_nonexistent_label_is_noop(tmp_path: Path) -> None:
    """Removing a label that was never created does not raise."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    wt_dir = tmp_path / "worktrees"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)
    # Should not raise.
    await mgr.remove("nonexistent")
