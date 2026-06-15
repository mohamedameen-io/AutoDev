"""C4: concurrency + corrective mis-assignment tests for :class:`WorktreePool`.

These tests pin the worktree<->task identity contract under concurrent
claim/release (the corrective-task fan-out path that produces ids like
``1.c4`` / ``1.c5``). They assert:

* N concurrent claims never hand the same pooled path to two tasks at once
  (no double-assignment of ``_claimed`` / ``_claim_task``),
* concurrent ``remove_per_task`` for every claimed task releases cleanly with
  no leaked worktree (every pooled path is back in the queue afterwards),
* the explicit ``_task_to_path`` index gives an O(1) direct lookup that stays
  consistent with ``_claim_task`` (path->task_id),
* ``remove_per_task`` on an unknown task_id logs the structured
  ``worktree_pool.remove_per_task_not_found`` warning and does NOT raise,
* ``release`` warns on a task_id / claim mismatch (explicit-identity guard).
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from orchestrator.worktree_pool import WorktreePool


def _init_git_repo(path: Path) -> None:
    """Initialize a minimal git repo with one committed file."""
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
async def test_concurrent_claim_release_no_leak_no_double_assign(
    tmp_path: Path,
) -> None:
    """N>=4 concurrent claims + concurrent remove_per_task: no leak, no share.

    Mirrors the corrective fan-out: several tasks (``1.c2`` … ``1.c5``)
    concurrently claim from a pool sized exactly to fit them, each writes a
    sentinel into its own worktree, then every task is concurrently removed.
    Asserts:
      * every claimed path is distinct (no two tasks hold the same path),
      * no path is double-tracked in ``_claim_task``,
      * after teardown the queue holds every pooled worktree again (no leak)
        and the per-claim maps are empty.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    n = 5
    pool = WorktreePool(main_repo=repo, pool_dir=tmp_path / "pool", size=n)
    await pool.cold_start()

    task_ids = [f"1.c{i}" for i in range(1, n + 1)]

    # Concurrent claims — the racy read-modify-write window.
    paths = await asyncio.gather(
        *(pool.claim(task_id=t) for t in task_ids)
    )

    # No two tasks share a claimed path.
    assert len(set(str(p) for p in paths)) == n, "two tasks got the same path"
    # Each task is recorded against exactly one distinct path.
    assert sorted(pool._claim_task.values()) == sorted(task_ids)
    assert len(pool._claim_task) == n
    # The explicit task->path index agrees with path->task_id.
    assert {pool._task_to_path[t] for t in task_ids} == {str(p) for p in paths}

    # Each task writes a sentinel into its own worktree — proves isolation.
    for t, p in zip(task_ids, paths):
        (p / "sentinel.txt").write_text(t)

    # Concurrent teardown via the corrective facade.
    await asyncio.gather(*(pool.remove_per_task(t) for t in task_ids))

    # No leak: every pooled worktree is back in the queue, nothing tracked.
    assert pool._available is not None
    assert pool._available.qsize() == n
    assert pool._claim_task == {}
    assert pool._task_to_path == {}
    assert pool._claimed == {}

    await pool.cleanup_all()


@pytest.mark.asyncio
async def test_remove_per_task_unknown_id_warns_no_raise(
    tmp_path: Path,
) -> None:
    """Removing an untracked task_id logs the not-found warning, no raise."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    pool = WorktreePool(main_repo=repo, pool_dir=tmp_path / "pool", size=1)
    await pool.cold_start()

    warnings: list[tuple[str, dict]] = []
    orig = pool._log.warning

    def _spy(event: str, **kw: object) -> None:
        warnings.append((event, kw))
        orig(event, **kw)

    pool._log.warning = _spy  # type: ignore[method-assign]

    # Never claimed — must not raise, must warn.
    await pool.remove_per_task("ghost.c9")

    assert any(
        e == "worktree_pool.remove_per_task_not_found" for e, _ in warnings
    ), f"expected not_found warning, got {[e for e, _ in warnings]}"

    await pool.cleanup_all()


@pytest.mark.asyncio
async def test_release_warns_on_task_identity_mismatch(tmp_path: Path) -> None:
    """release() with the wrong task_id for a path warns (explicit identity)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    pool = WorktreePool(main_repo=repo, pool_dir=tmp_path / "pool", size=1)
    await pool.cold_start()

    p = await pool.claim(task_id="owner.c1")

    warnings: list[str] = []
    orig = pool._log.warning

    def _spy(event: str, **kw: object) -> None:
        warnings.append(event)
        orig(event, **kw)

    pool._log.warning = _spy  # type: ignore[method-assign]

    # Release the SAME path but claiming it belongs to a different task.
    await pool.release(p, task_id="impostor.c2")

    assert "worktree_pool.release.task_identity_mismatch" in warnings

    await pool.cleanup_all()


@pytest.mark.asyncio
async def test_concurrent_claim_release_churn_consistency(
    tmp_path: Path,
) -> None:
    """Interleaved claim/remove churn keeps _claim_task / _task_to_path in sync.

    Hammers the pool with repeated claim->remove cycles across many tasks so
    that any non-atomic read-modify-write between the two maps would surface
    as a divergence (a tracked task with no reverse entry, or vice versa).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    n = 4
    pool = WorktreePool(main_repo=repo, pool_dir=tmp_path / "pool", size=n)
    await pool.cold_start()

    async def cycle(task_id: str) -> None:
        await pool.claim(task_id=task_id)
        await asyncio.sleep(0)  # yield, widen any race window
        await pool.remove_per_task(task_id)

    for _round in range(3):
        await asyncio.gather(
            *(cycle(f"r{_round}.c{i}") for i in range(n))
        )
        # Between rounds everything must be released and consistent.
        assert pool._claim_task == {}
        assert pool._task_to_path == {}
        assert pool._available is not None
        assert pool._available.qsize() == n

    await pool.cleanup_all()
