"""Tests for :mod:`src.state.plan_manager`."""

from __future__ import annotations

import asyncio
import datetime as _dt
from pathlib import Path

import pytest

from errors import PlanConcurrentModificationError
from state.plan_manager import PlanManager
from state.schemas import Phase, Plan, Task


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan() -> Plan:
    return Plan(
        plan_id="p-test",
        spec_hash="cafebabe",
        phases=[
            Phase(
                id="1",
                title="Setup",
                tasks=[
                    Task(id="1.1", phase_id="1", title="task a", description="do a"),
                    Task(id="1.2", phase_id="1", title="task b", description="do b"),
                ],
            ),
            Phase(
                id="2",
                title="Finalize",
                tasks=[
                    Task(id="2.1", phase_id="2", title="task c", description="do c"),
                ],
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
    )


@pytest.mark.asyncio
async def test_load_returns_none_when_empty(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    assert await pm.load() is None


@pytest.mark.asyncio
async def test_init_and_load_round_trip(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    loaded = await pm.load()
    assert loaded is not None
    assert loaded.plan_id == "p-test"
    assert len(loaded.phases) == 2


@pytest.mark.asyncio
async def test_init_plan_twice_raises(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    with pytest.raises(PlanConcurrentModificationError):
        await pm.init_plan(_mk_plan())


@pytest.mark.asyncio
async def test_save_overwrites(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    # Create an edited copy with an extra task, save it.
    loaded = await pm.load()
    assert loaded is not None
    loaded.phases[0].tasks.append(
        Task(id="1.3", phase_id="1", title="new", description="new")
    )
    await pm.save(loaded)
    reloaded = await pm.load()
    assert reloaded is not None
    assert len(reloaded.phases[0].tasks) == 3


@pytest.mark.asyncio
async def test_update_task_status_enforces_fsm(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    # pending -> complete is invalid (must go through in_progress etc.)
    with pytest.raises(ValueError):
        await pm.update_task_status("1.1", "complete")
    # Valid transition.
    t = await pm.update_task_status("1.1", "in_progress")
    assert t.status == "in_progress"


@pytest.mark.asyncio
async def test_update_task_status_unknown_task_raises(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    with pytest.raises(PlanConcurrentModificationError):
        await pm.update_task_status("bogus", "in_progress")


@pytest.mark.asyncio
async def test_get_task_and_next_pending(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    t = await pm.get_task("1.1")
    assert t is not None and t.id == "1.1"
    nxt = await pm.next_pending_task()
    assert nxt is not None and nxt.id == "1.1"
    # Move 1.1 out of pending; next should be 1.2.
    await pm.update_task_status("1.1", "in_progress")
    nxt2 = await pm.next_pending_task()
    assert nxt2 is not None and nxt2.id == "1.2"


@pytest.mark.asyncio
async def test_retry_counting(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    await pm.update_task_status("1.1", "in_progress")
    assert await pm.mark_task_retry("1.1") == 1
    assert await pm.mark_task_retry("1.1") == 2
    t = await pm.get_task("1.1")
    assert t is not None and t.retry_count == 2


@pytest.mark.asyncio
async def test_mark_escalated_flag(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    await pm.update_task_status("1.1", "in_progress")
    await pm.mark_escalated("1.1")
    t = await pm.get_task("1.1")
    assert t is not None and t.escalated is True


@pytest.mark.asyncio
async def test_concurrent_writers_serialized(tmp_path: Path) -> None:
    """Two asyncio tasks hammering update_task_status must not corrupt the ledger."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    await pm.update_task_status("1.1", "in_progress")
    await pm.update_task_status("1.2", "in_progress")

    async def bump(task_id: str, n: int) -> None:
        for _ in range(n):
            await pm.mark_task_retry(task_id)

    await asyncio.gather(bump("1.1", 3), bump("1.2", 3))

    t1 = await pm.get_task("1.1")
    t2 = await pm.get_task("1.2")
    assert t1 is not None and t1.retry_count == 3
    assert t2 is not None and t2.retry_count == 3


@pytest.mark.asyncio
async def test_snapshot_fast_path_matches_replay(tmp_path: Path) -> None:
    """After a save, load returns the snapshotted plan without replaying from scratch."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    await pm.update_task_status("1.1", "in_progress")
    await pm.update_task_status("1.1", "coded")
    await pm.update_task_status("1.1", "auto_gated")
    # Force a fresh PlanManager instance and make sure load works.
    pm2 = PlanManager(tmp_path, session_id="s2")
    loaded = await pm2.load()
    assert loaded is not None
    assert loaded.phases[0].tasks[0].status == "auto_gated"
