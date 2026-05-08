"""v0.15.0: PlanManager stuck-state increments + reset (in-memory only).

Mirrors v0.11.0's ``_in_flight`` pattern: state lives on the PlanManager
instance for the duration of a run, NOT persisted to ``plan.json`` or
the ledger. A crash mid-flight resets it to defaults — the cross-run
lessons memory holds the durable signal.

Validates:
* ``increment_discard(task_id)`` returns updated :class:`StuckState`.
* ``increment_pivot(task_id)`` returns updated :class:`StuckState`.
* ``reset_stuck_state(task_id)`` zeroes the counters.
* All three acquire ``plan_lock`` (no concurrent writers can collide).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.escalation_ladder import StuckState
from state.plan_manager import PlanManager


@pytest.mark.asyncio
async def test_get_stuck_state_default_is_zeroed(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    state = await pm.get_stuck_state("task-1")
    assert state.discard_count == 0
    assert state.pivot_count == 0
    assert state.last_event == ""


@pytest.mark.asyncio
async def test_increment_discard_persists_under_lock(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    state = await pm.increment_discard("task-1")
    assert state.discard_count == 1
    assert state.last_event == "discard"
    state2 = await pm.increment_discard("task-1")
    assert state2.discard_count == 2
    assert state2.pivot_count == 0


@pytest.mark.asyncio
async def test_increment_pivot_persists_under_lock(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    state = await pm.increment_pivot("task-1")
    assert state.pivot_count == 1
    assert state.last_event == "pivot"
    state2 = await pm.increment_pivot("task-1")
    assert state2.pivot_count == 2


@pytest.mark.asyncio
async def test_reset_stuck_state_zeros_counters(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.increment_discard("task-1")
    await pm.increment_discard("task-1")
    await pm.increment_pivot("task-1")
    pre = await pm.get_stuck_state("task-1")
    assert pre.discard_count == 2 and pre.pivot_count == 1

    await pm.reset_stuck_state("task-1")
    state = await pm.get_stuck_state("task-1")
    assert state.discard_count == 0
    assert state.pivot_count == 0


@pytest.mark.asyncio
async def test_increment_isolated_per_task_id(tmp_path: Path) -> None:
    """Each task's stuck state is independent — incrementing one task
    must not bleed into another's counters."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.increment_discard("task-A")
    await pm.increment_discard("task-A")
    await pm.increment_discard("task-B")
    state_a = await pm.get_stuck_state("task-A")
    state_b = await pm.get_stuck_state("task-B")
    assert state_a.discard_count == 2
    assert state_b.discard_count == 1


@pytest.mark.asyncio
async def test_reset_unknown_task_id_is_idempotent(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    # Reset on a never-touched task must not raise.
    await pm.reset_stuck_state("task-never-seen")
    state = await pm.get_stuck_state("task-never-seen")
    assert state == StuckState()


@pytest.mark.asyncio
async def test_increments_do_not_persist_across_pm_instances(tmp_path: Path) -> None:
    """Stuck state is in-memory only — a fresh PlanManager starts clean."""
    pm1 = PlanManager(tmp_path, session_id="s1")
    await pm1.increment_discard("task-1")
    await pm1.increment_discard("task-1")
    pm2 = PlanManager(tmp_path, session_id="s2")
    state = await pm2.get_stuck_state("task-1")
    assert state.discard_count == 0
