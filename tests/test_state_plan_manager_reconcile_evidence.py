"""v0.22.2 B3 regression: ``reconcile_evidence_vs_ledger`` heals orphan work.

The 2026-05-09 Unity stall (D-3 finding): ``write_evidence`` runs at
``execute_phase.py:1771`` BEFORE ``update_task_status("coded")`` at
``:1818``. A crash in between leaves the developer's success on disk
but no ledger record. Pre-B3 the resume reaper then reverted the task
to ``pending`` and re-ran from scratch, discarding the work.

B3 closes the loop: an ``attempt_started`` marker emitted before the
developer dispatch lets resume detect "evidence + marker, no coded
op" and auto-promote to ``coded``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from state.evidence import write_evidence
from state.ledger import append_entry
from state.lockfile import plan_lock
from state.plan_manager import PlanManager
from state.schemas import CoderEvidence, Phase, Plan, Task


def _build_plan(*statuses: str) -> Plan:
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
async def test_reconcile_no_op_on_clean_workspace(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="t")
    plan = _build_plan("pending")
    await pm.init_plan(plan)
    result = await pm.reconcile_evidence_vs_ledger()
    assert result == {"promoted": [], "discrepancies": []}


@pytest.mark.asyncio
async def test_orphan_evidence_with_marker_promoted(tmp_path: Path) -> None:
    """attempt_started + success evidence + no coded op → auto-promote."""
    pm = PlanManager(tmp_path, session_id="t")
    plan = _build_plan("pending")
    await pm.init_plan(plan)

    # Simulate the crash: marker + evidence written, but no coded op.
    async with plan_lock(tmp_path):
        await append_entry(
            tmp_path,
            op="attempt_started",
            payload={
                "task_id": "1.1",
                "attempt_n": 0,
                "started_at": "2026-05-10T00:00:00+00:00",
                "session_id": "t",
            },
            session_id="t",
        )
    ev = CoderEvidence(
        task_id="1.1",
        diff="--- a/f\n+++ b/f\n",
        files_changed=["f.py"],
        success=True,
    )
    await write_evidence(tmp_path, "1.1", ev)

    result = await pm.reconcile_evidence_vs_ledger()
    assert result["promoted"] == ["1.1"]
    assert result["discrepancies"] == []

    # Task is now coded.
    refreshed = await pm.load()
    assert refreshed is not None
    assert refreshed.phases[0].tasks[0].status == "coded"


@pytest.mark.asyncio
async def test_evidence_without_marker_is_discrepancy(tmp_path: Path) -> None:
    """Evidence without an attempt_started marker is flagged for operator."""
    pm = PlanManager(tmp_path, session_id="t")
    plan = _build_plan("pending")
    await pm.init_plan(plan)
    ev = CoderEvidence(task_id="1.1", success=True)
    await write_evidence(tmp_path, "1.1", ev)
    result = await pm.reconcile_evidence_vs_ledger()
    assert result["promoted"] == []
    assert any(
        d["task_id"] == "1.1" and "without_attempt_started" in d["reason"]
        for d in result["discrepancies"]
    )


@pytest.mark.asyncio
async def test_failed_evidence_skipped(tmp_path: Path) -> None:
    """Evidence with success=false is not promoted."""
    pm = PlanManager(tmp_path, session_id="t")
    plan = _build_plan("pending")
    await pm.init_plan(plan)
    async with plan_lock(tmp_path):
        await append_entry(
            tmp_path,
            op="attempt_started",
            payload={"task_id": "1.1", "attempt_n": 0, "started_at": "x", "session_id": "t"},
            session_id="t",
        )
    ev = CoderEvidence(task_id="1.1", success=False)
    await write_evidence(tmp_path, "1.1", ev)
    result = await pm.reconcile_evidence_vs_ledger()
    assert result == {"promoted": [], "discrepancies": []}


@pytest.mark.asyncio
async def test_idempotent_when_ledger_caught_up(tmp_path: Path) -> None:
    """When the task is already coded, reconcile is a no-op (idempotent)."""
    pm = PlanManager(tmp_path, session_id="t")
    plan = _build_plan("pending")
    await pm.init_plan(plan)
    async with plan_lock(tmp_path):
        await append_entry(
            tmp_path,
            op="attempt_started",
            payload={"task_id": "1.1", "attempt_n": 0, "started_at": "x", "session_id": "t"},
            session_id="t",
        )
    ev = CoderEvidence(task_id="1.1", success=True)
    await write_evidence(tmp_path, "1.1", ev)
    # Drive to coded normally.
    await pm.update_task_status("1.1", "in_progress")
    await pm.update_task_status("1.1", "coded")
    # Second reconcile: no-op.
    result = await pm.reconcile_evidence_vs_ledger()
    assert result == {"promoted": [], "discrepancies": []}
