"""v0.22.2 B1 regression: ``PlanManager.reap_orphans`` recovers wedged tasks.

D-2 (the 2026-05-09 Unity stall) showed that interrupted runs leave
tasks frozen in non-terminal-non-pending states (``coded``,
``in_progress``, ``reviewed``, ``tested``, ``tournamented``) — the
dispatcher's pending-only filter cannot pick them up, so the run
appears wedged. ``reap_orphans`` reverts each to ``pending`` via the
existing ``revert_task_to_pending`` primitive.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from state.plan_manager import PlanManager
from state.schemas import Phase, Plan, Task


def _build_plan(*statuses: str) -> Plan:
    """Construct a plan with one phase whose tasks have the given statuses."""
    return Plan(
        plan_id="p1",
        spec_hash="h1",
        created_at="2026-05-10T00:00:00",
        updated_at="2026-05-10T00:00:00",
        phases=[
            Phase(
                id="1",
                title="P1",
                tasks=[
                    Task(
                        id=f"1.{i+1}",
                        phase_id="1",
                        title=f"t{i}",
                        description="d",
                        files=[],
                        status=s,  # type: ignore[arg-type]
                    )
                    for i, s in enumerate(statuses)
                ],
            ),
        ],
    )


@pytest.mark.asyncio
async def test_reap_orphans_no_op_on_empty_plan(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="t")
    reaped = await pm.reap_orphans()
    assert reaped == []


@pytest.mark.asyncio
async def test_reap_orphans_no_op_on_clean_plan(tmp_path: Path) -> None:
    """A plan with only pending+terminal tasks reaps nothing."""
    pm = PlanManager(tmp_path, session_id="t")
    plan = _build_plan("pending", "complete", "blocked", "skipped")
    await pm.init_plan(plan)
    reaped = await pm.reap_orphans()
    assert reaped == []


@pytest.mark.asyncio
async def test_reap_orphans_reverts_in_progress(tmp_path: Path) -> None:
    """A task wedged in ``in_progress`` gets reverted to ``pending``."""
    pm = PlanManager(tmp_path, session_id="t")
    plan = _build_plan("in_progress")
    await pm.init_plan(plan)
    reaped = await pm.reap_orphans()
    assert reaped == ["1.1"]
    refreshed = await pm.load()
    assert refreshed is not None
    assert refreshed.phases[0].tasks[0].status == "pending"


@pytest.mark.asyncio
async def test_reap_orphans_reverts_all_intermediate_states(
    tmp_path: Path,
) -> None:
    """Each non-terminal-non-pending status is recovered."""
    pm = PlanManager(tmp_path, session_id="t")
    statuses = ("coded", "auto_gated", "reviewed", "tested", "tournamented")
    plan = _build_plan(*statuses)
    await pm.init_plan(plan)
    reaped = await pm.reap_orphans()
    assert sorted(reaped) == [f"1.{i+1}" for i in range(len(statuses))]
    refreshed = await pm.load()
    assert refreshed is not None
    assert all(t.status == "pending" for t in refreshed.phases[0].tasks)


@pytest.mark.asyncio
async def test_reap_orphans_idempotent(tmp_path: Path) -> None:
    """Calling twice is a no-op the second time."""
    pm = PlanManager(tmp_path, session_id="t")
    plan = _build_plan("coded", "in_progress")
    await pm.init_plan(plan)
    first = await pm.reap_orphans()
    second = await pm.reap_orphans()
    assert sorted(first) == ["1.1", "1.2"]
    assert second == []


@pytest.mark.asyncio
async def test_reap_orphans_preserves_pending_and_terminal(
    tmp_path: Path,
) -> None:
    """Only the wedged tasks are reverted; pending/terminal stay put."""
    pm = PlanManager(tmp_path, session_id="t")
    plan = _build_plan("pending", "in_progress", "complete", "coded", "blocked")
    await pm.init_plan(plan)
    reaped = await pm.reap_orphans()
    assert sorted(reaped) == ["1.2", "1.4"]
    refreshed = await pm.load()
    assert refreshed is not None
    statuses = [t.status for t in refreshed.phases[0].tasks]
    # Originals: pending, in_progress, complete, coded, blocked
    # After reap: pending, pending,     complete, pending, blocked
    assert statuses == ["pending", "pending", "complete", "pending", "blocked"]
