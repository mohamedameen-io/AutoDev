"""Tests for :class:`WorktreeManager` using real git init + worktree operations."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.worktree import WorktreeError, WorktreeManager, _run_git


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


# ── New coverage tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_worktree_properties(tmp_path: Path) -> None:
    """Verify main_repo and tournament_dir properties return correct paths."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    wt_dir = tmp_path / "worktrees"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)

    assert mgr.main_repo == repo
    assert mgr.tournament_dir == wt_dir


@pytest.mark.asyncio
async def test_remove_nonexistent_worktree_just_prunes(tmp_path: Path) -> None:
    """Calling remove on a label whose path doesn't exist just prunes git metadata."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    wt_dir = tmp_path / "worktrees"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)

    # Create then manually delete the directory (simulating partial failure).
    wt = await mgr.create("stale", base_ref="HEAD")
    assert wt.exists()
    import shutil
    shutil.rmtree(wt)
    assert not wt.exists()

    # remove() should not raise — it just prunes stale metadata.
    await mgr.remove("stale")


@pytest.mark.asyncio
async def test_remove_with_force_flag(tmp_path: Path) -> None:
    """Create a worktree, write uncommitted files, then force-remove it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    wt_dir = tmp_path / "worktrees"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)

    wt = await mgr.create("dirty", base_ref="HEAD")
    # Modify an existing file and add a new one (uncommitted changes).
    (wt / "README.md").write_text("modified\n")
    (wt / "extra.py").write_text("x = 1\n")

    await mgr.remove("dirty", force=True)
    assert not wt.exists()


@pytest.mark.asyncio
async def test_remove_dirty_without_force_triggers_force_fallback(tmp_path: Path) -> None:
    """remove(force=False) on a dirty worktree triggers _force_remove fallback."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    wt_dir = tmp_path / "worktrees"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)

    wt = await mgr.create("dirty2", base_ref="HEAD")
    (wt / "README.md").write_text("modified\n")

    # force=False, but dirty worktree -> git worktree remove fails ->
    # code falls through to _force_remove.
    await mgr.remove("dirty2", force=False)
    assert not wt.exists()


@pytest.mark.asyncio
async def test_cleanup_all_empty_dir(tmp_path: Path) -> None:
    """cleanup_all on a tournament_dir that doesn't exist is a noop."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    wt_dir = tmp_path / "nonexistent_worktrees"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)
    assert not wt_dir.exists()

    # Must not raise.
    await mgr.cleanup_all()


@pytest.mark.asyncio
async def test_cleanup_all_with_worktrees(tmp_path: Path) -> None:
    """Create 2 worktrees, cleanup_all removes both and the tournament dir."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    wt_dir = tmp_path / "worktrees"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)

    wt_a = await mgr.create("a", base_ref="HEAD")
    wt_b = await mgr.create("b", base_ref="HEAD")
    # Dirty both worktrees.
    (wt_a / "file_a.py").write_text("a = 1\n")
    (wt_b / "file_b.py").write_text("b = 2\n")

    await mgr.cleanup_all()
    assert not wt_a.exists()
    assert not wt_b.exists()
    assert not wt_dir.exists()


@pytest.mark.asyncio
async def test_get_diff_vs_base_nonexistent_raises(tmp_path: Path) -> None:
    """get_diff_vs_base on a non-existent worktree path raises WorktreeError."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    wt_dir = tmp_path / "worktrees"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)

    with pytest.raises(WorktreeError, match="does not exist"):
        await mgr.get_diff_vs_base(wt_dir / "ghost")


@pytest.mark.asyncio
async def test_get_diff_with_untracked_files(tmp_path: Path) -> None:
    """An untracked file in a worktree appears in the diff output."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    wt_dir = tmp_path / "worktrees"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)

    wt = await mgr.create("untracked", base_ref="HEAD")
    # Create a new untracked file (never git-added).
    (wt / "brand_new.py").write_text("def hello(): return 'world'\n")

    diff = await mgr.get_diff_vs_base(wt)
    assert "brand_new.py" in diff
    assert "hello" in diff

    await mgr.cleanup_all()


@pytest.mark.asyncio
async def test_apply_patch_empty_diff_noop(tmp_path: Path) -> None:
    """apply_patch_to_main is a noop when the worktree has no changes."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    wt_dir = tmp_path / "worktrees"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)

    wt = await mgr.create("clean", base_ref="HEAD")
    # No modifications — apply should silently succeed.
    await mgr.apply_patch_to_main(wt)

    # Main repo is unchanged.
    assert (repo / "README.md").read_text() == "# test\n"

    await mgr.cleanup_all()


@pytest.mark.asyncio
async def test_apply_patch_success(tmp_path: Path) -> None:
    """Modify a file in the worktree, apply to main, verify main has the change."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    wt_dir = tmp_path / "worktrees"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)

    wt = await mgr.create("winner", base_ref="HEAD")
    # Modify tracked file in worktree.
    (wt / "README.md").write_text("# updated by tournament winner\n")

    await mgr.apply_patch_to_main(wt)

    # Main repo should now have the change.
    assert (repo / "README.md").read_text() == "# updated by tournament winner\n"

    await mgr.cleanup_all()


# ── _run_git edge-case tests (mocked) ─────────────────────────────────


@pytest.mark.asyncio
async def test_run_git_timeout_raises(tmp_path: Path) -> None:
    """_run_git raises WorktreeError when the subprocess times out."""
    # Create a mock process whose communicate() hangs until timeout.
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
    mock_proc.kill = AsyncMock()
    mock_proc.wait = AsyncMock()

    with patch("orchestrator.worktree.asyncio.create_subprocess_exec", return_value=mock_proc):
        with pytest.raises(WorktreeError, match="timed out"):
            await _run_git(tmp_path, ["status"], timeout_s=0.01)


@pytest.mark.asyncio
async def test_run_git_launch_failure(tmp_path: Path) -> None:
    """_run_git raises WorktreeError when git binary is not found."""
    with patch(
        "orchestrator.worktree.asyncio.create_subprocess_exec",
        side_effect=FileNotFoundError("git not found"),
    ):
        with pytest.raises(WorktreeError, match="failed to launch git"):
            await _run_git(tmp_path, ["status"])
