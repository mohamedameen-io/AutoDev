"""v0.21.0 A1: tests for :class:`orchestrator.worktree_pool.WorktreePool`.

Covers:
* cold-start concurrency (N worktrees pre-created),
* claim/release lifecycle (queue drain + recycle),
* fallback to lazy create on pool exhaustion,
* reset idempotence (release on already-baseline worktree),
* cleanup_all removes pool dir.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orchestrator.worktree_pool import WorktreePool


def _init_git_repo(path: Path) -> None:
    """Initialize a minimal git repo with one committed file."""
    subprocess.run(
        ["git", "init", str(path)], check=True, capture_output=True
    )
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
    subprocess.run(
        ["git", "add", "."], cwd=str(path), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )


@pytest.mark.asyncio
async def test_cold_start_creates_n_worktrees(tmp_path: Path) -> None:
    """``cold_start(3)`` creates 3 worktrees and captures baseline SHA."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    pool = WorktreePool(
        main_repo=repo,
        pool_dir=tmp_path / "pool",
        size=3,
    )
    await pool.cold_start()

    # Baseline captured.
    assert pool.baseline_commit
    # 3 worktrees on disk.
    on_disk = sorted(p.name for p in (tmp_path / "pool").iterdir() if p.is_dir())
    assert on_disk == ["pool-0", "pool-1", "pool-2"]

    await pool.cleanup_all()


@pytest.mark.asyncio
async def test_claim_release_lifecycle(tmp_path: Path) -> None:
    """Claim drains queue; release returns to queue (recycle)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    pool = WorktreePool(
        main_repo=repo,
        pool_dir=tmp_path / "pool",
        size=2,
    )
    await pool.cold_start()

    p1 = await pool.claim(task_id="t1")
    p2 = await pool.claim(task_id="t2")
    assert p1.exists() and p2.exists()
    assert p1 != p2

    # Pool is now drained — release p1 and re-claim should return p1.
    await pool.release(p1, task_id="t1")
    p3 = await pool.claim(task_id="t3")
    assert p3 == p1

    await pool.cleanup_all()


@pytest.mark.asyncio
async def test_overflow_fallback_to_lazy_create(tmp_path: Path) -> None:
    """When pool is exhausted, claim creates an overflow worktree lazily."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    pool = WorktreePool(
        main_repo=repo,
        pool_dir=tmp_path / "pool",
        size=1,
    )
    await pool.cold_start()

    p1 = await pool.claim(task_id="t1")
    p2 = await pool.claim(task_id="t2")  # forces overflow
    assert p1.exists() and p2.exists()
    assert p1 != p2
    # Overflow worktree lives under tasks/<id>.
    assert "tasks" in p2.parts and "t2" in p2.parts

    # Releasing the overflow worktree REMOVES it (doesn't re-queue).
    await pool.release(p2, task_id="t2")
    assert not p2.exists()

    await pool.cleanup_all()


@pytest.mark.asyncio
async def test_release_resets_to_baseline(tmp_path: Path) -> None:
    """Release wipes uncommitted edits via ``git reset --hard``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    pool = WorktreePool(
        main_repo=repo,
        pool_dir=tmp_path / "pool",
        size=1,
    )
    await pool.cold_start()
    p1 = await pool.claim(task_id="t1")

    # Dirty the worktree.
    (p1 / "scratch.txt").write_text("residue\n")
    (p1 / "README.md").write_text("# overwritten\n")
    assert (p1 / "scratch.txt").exists()

    await pool.release(p1, task_id="t1")
    # Re-claim should see clean state — same path, same baseline.
    p2 = await pool.claim(task_id="t2")
    assert p2 == p1
    assert not (p2 / "scratch.txt").exists()
    assert (p2 / "README.md").read_text() == "# test\n"

    await pool.cleanup_all()


@pytest.mark.asyncio
async def test_release_idempotent_on_clean_baseline(tmp_path: Path) -> None:
    """Releasing an unmodified claim is a no-op (no error)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    pool = WorktreePool(
        main_repo=repo,
        pool_dir=tmp_path / "pool",
        size=1,
    )
    await pool.cold_start()
    p = await pool.claim(task_id="t1")
    # No edits — release straight back.
    await pool.release(p, task_id="t1")
    p2 = await pool.claim(task_id="t2")
    assert p2 == p

    await pool.cleanup_all()


@pytest.mark.asyncio
async def test_cleanup_all_removes_pool_dir(tmp_path: Path) -> None:
    """``cleanup_all`` deletes every worktree AND the pool dir."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    pool_dir = tmp_path / "pool"
    pool = WorktreePool(
        main_repo=repo,
        pool_dir=pool_dir,
        size=2,
    )
    await pool.cold_start()
    assert pool_dir.exists()
    await pool.cleanup_all()
    assert not pool_dir.exists()

    # Idempotent — second call doesn't crash.
    await pool.cleanup_all()


@pytest.mark.asyncio
async def test_pool_compatible_facade_create_remove_per_task(
    tmp_path: Path,
) -> None:
    """``create_per_task`` / ``remove_per_task`` on the pool match manager API."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    pool = WorktreePool(
        main_repo=repo,
        pool_dir=tmp_path / "pool",
        size=1,  # size=1 forces deterministic recycle path
    )
    await pool.cold_start()
    p = await pool.create_per_task("t-alpha")
    assert p.exists()
    # remove_per_task → release lifecycle (queue or remove for overflow).
    await pool.remove_per_task("t-alpha")
    # Re-claim should give us back the same pool slot.
    p2 = await pool.create_per_task("t-beta")
    assert p2 == p
    await pool.cleanup_all()


@pytest.mark.asyncio
async def test_cold_start_zero_size_skips(tmp_path: Path) -> None:
    """``cold_start`` with size=0 captures baseline but creates nothing."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    pool = WorktreePool(
        main_repo=repo,
        pool_dir=tmp_path / "pool",
        size=0,
    )
    await pool.cold_start()
    pool_dir = tmp_path / "pool"
    if pool_dir.exists():
        # Manager init may create the dir, but no worktrees inside.
        assert not any(p.is_dir() for p in pool_dir.iterdir())

    # claim() with empty queue still works via overflow.
    p = await pool.claim(task_id="t-overflow")
    assert p.exists()
    await pool.release(p, task_id="t-overflow")

    await pool.cleanup_all()
