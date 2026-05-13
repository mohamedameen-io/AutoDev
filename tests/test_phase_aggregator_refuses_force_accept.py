"""Tests for v0.30.0 Bug 3: phase aggregator refuses to auto-accept any
phase containing a task with ``block_reason_class="infrastructure"``.

This generalises the v0.29.0 Bug 7 quarantined-check (which only fired
on tasks parked at the non-terminal ``quarantined`` status). The
production stall this guard exists to prevent: a phase whose only
remaining failures are typed-infrastructure blocks (timeouts, gateway
errors, transient network issues) being silently force-accepted by the
phase-review tournament after the operator-recoverable signal is
already on the ledger.

Coverage:

  * ``test_phase_with_infrastructure_blocked_task_paused_not_accepted``
    — happy halt: one task ``blocked`` with
    ``block_reason_class="infrastructure"``, the rest complete; the
    aggregator must park the phase at ``review_status="paused"`` and
    NOT fire the tournament.
  * ``test_phase_with_only_verdict_blocks_still_accepts`` — regression:
    a phase whose only blocks are ``"verdict"`` class still flows
    through the normal review path (here: the tournament is
    monkeypatched to accept).
  * ``test_phase_with_only_cap_blocks_still_accepts`` — regression:
    same shape for the ``"cap"`` class. Distinct from infrastructure
    because requeueing without raising the cap won't help.
  * ``test_phase_paused_resumes_to_accept_after_infra_resolved`` — the
    end-to-end recovery edge: park the phase at ``"paused"`` with the
    infrastructure-blocked task already requeued + completed; observe
    :meth:`Orchestrator.resume` clears the paused state and re-fires
    the tournament fresh.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator import execute_phase as ep
from state.plan_manager import PlanManager
from state.schemas import AcceptanceCriterion, Phase, Plan, Task

from stub_adapter import StubAdapter, ok


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_two_task_phase_plan() -> Plan:
    """One phase, two tasks — leaves room to walk one to ``complete`` and
    the other into ``blocked`` with a typed ``block_reason_class``."""
    return Plan(
        plan_id="p-bug3",
        spec_hash="0123456789abcdef",
        phases=[
            Phase(
                id="1",
                title="Investigate",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="t1",
                        description="d1",
                        complexity="medium",
                    ),
                    Task(
                        id="1.2",
                        phase_id="1",
                        title="t2",
                        description="d2",
                        complexity="medium",
                    ),
                ],
                acceptance=[
                    AcceptanceCriterion(id="ph-ac-1", description="all good")
                ],
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
        complexity="medium",
    )


def _make_orch(cwd: Path) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.phase_review.enabled = True
    cfg.tournaments.auto_disable_for_models = []
    cfg.tournaments.impl.enabled = False
    cfg.tournaments.plan.enabled = False
    registry = build_registry(cfg)
    adapter = StubAdapter({"explorer": ok("ok")})
    return Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-test-bug3",
    )


async def _walk_to_complete(pm: PlanManager, task_id: str) -> None:
    """Drive a pending task all the way to ``complete`` via the canonical
    FSM edges. Used by every test below to set up the
    "all-other-tasks-done" precondition."""
    for s in (
        "in_progress",
        "coded",
        "auto_gated",
        "reviewed",
        "tested",
        "tournamented",
        "complete",
    ):
        await pm.update_task_status(task_id, s)


async def _block_task(
    pm: PlanManager,
    task_id: str,
    *,
    block_reason_class: str,
    blocked_reason: str,
) -> None:
    """Walk a task pending -> in_progress -> blocked with the given
    typed class. Mirrors the meta payload :func:`_execute_one_worker`
    builds at the worker-exception block site."""
    await pm.update_task_status(task_id, "in_progress")
    await pm.update_task_status(
        task_id,
        "blocked",
        meta={
            "blocked_reason": blocked_reason,
            "block_reason_class": block_reason_class,
        },
    )


@pytest.mark.asyncio
async def test_phase_with_infrastructure_blocked_task_paused_not_accepted(
    tmp_path: Path,
) -> None:
    """One task ``blocked`` with ``block_reason_class="infrastructure"``,
    the other ``complete`` -> ``_maybe_run_phase_review`` parks the
    phase at ``review_status="paused"`` and does NOT fire the
    tournament.

    This is the production stall guard: the v0.29.0 Bug 7 check only
    fired on the non-terminal ``quarantined`` status, but typed
    ``"infrastructure"`` blocks (also operator-recoverable) reach a
    terminal ``blocked`` state via the worker-exception path. Without
    this generalisation, the aggregator would observe "all terminal"
    and force-accept on the halt path.
    """
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_two_task_phase_plan())
    await _walk_to_complete(pm, "1.2")
    await _block_task(
        pm,
        "1.1",
        block_reason_class="infrastructure",
        blocked_reason="qa_gate_timeout: simulated network drop",
    )

    orch = _make_orch(tmp_path)

    await ep._maybe_run_phase_review(orch, "1")

    plan = await orch.plan_manager.load()
    assert plan is not None
    phase = plan.phases[0]
    assert phase.review_status == "paused"
    # Blocked task is unchanged (still infrastructure-class blocked).
    assert phase.tasks[0].status == "blocked"
    assert phase.tasks[0].block_reason_class == "infrastructure"


@pytest.mark.asyncio
async def test_phase_with_only_verdict_blocks_still_accepts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a phase whose only blocks are ``"verdict"`` class
    flows through the normal phase-review path (no infrastructure-class
    pause). The tournament is monkeypatched to accept so we can observe
    the aggregator did not short-circuit.

    Critical: ``"verdict"`` blocks are not safely-requeueable; the
    aggregator must NOT park them at ``"paused"`` — the operator
    needs the review tournament to inject corrective tasks.
    """
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_two_task_phase_plan())
    await _walk_to_complete(pm, "1.2")
    await _block_task(
        pm,
        "1.1",
        block_reason_class="verdict",
        blocked_reason="qa_gate_encoding_error: bad utf-8 in patch",
    )

    orch = _make_orch(tmp_path)

    review_calls: list[str] = []

    async def _fake_run_phase_review(
        orch_arg: Any, phase: Phase
    ) -> None:
        review_calls.append(phase.id)
        await orch_arg.plan_manager.update_phase_meta(
            phase.id, review_status="accepted"
        )

    monkeypatch.setattr(ep, "_run_phase_review", _fake_run_phase_review)

    await ep._maybe_run_phase_review(orch, "1")

    plan = await orch.plan_manager.load()
    assert plan is not None
    phase = plan.phases[0]
    # Tournament fired and accepted — pause guard did NOT short-circuit.
    assert review_calls == ["1"]
    assert phase.review_status == "accepted"


@pytest.mark.asyncio
async def test_phase_with_only_cap_blocks_still_accepts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a phase whose only blocks are ``"cap"`` class flows
    through normal review. Cap blocks (agent ate its turns/tokens) are
    distinct from infrastructure: requeueing without raising the cap
    just re-burns the budget, so they go through review like verdict.
    """
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_two_task_phase_plan())
    await _walk_to_complete(pm, "1.2")
    await _block_task(
        pm,
        "1.1",
        block_reason_class="cap",
        blocked_reason="guardrail: turn cap exceeded",
    )

    orch = _make_orch(tmp_path)

    review_calls: list[str] = []

    async def _fake_run_phase_review(
        orch_arg: Any, phase: Phase
    ) -> None:
        review_calls.append(phase.id)
        await orch_arg.plan_manager.update_phase_meta(
            phase.id, review_status="accepted"
        )

    monkeypatch.setattr(ep, "_run_phase_review", _fake_run_phase_review)

    await ep._maybe_run_phase_review(orch, "1")

    plan = await orch.plan_manager.load()
    assert plan is not None
    phase = plan.phases[0]
    assert review_calls == ["1"]
    assert phase.review_status == "accepted"


@pytest.mark.asyncio
async def test_phase_paused_resumes_to_accept_after_infra_resolved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end recovery edge: a phase parked at
    ``review_status="paused"`` with the infrastructure-blocked task
    already requeued + walked to ``complete`` (operator ran
    ``autodev requeue --infrastructure`` then the worker landed it).
    :meth:`Orchestrator.resume` must clear the paused state and re-
    fire the phase-review tournament.

    The tournament itself is monkeypatched to a no-op that stamps
    ``"accepted"`` so we observe the resume path drove review through
    to conclusion. Mirrors the quarantined-resume regression test in
    ``test_task_quarantined_status.py`` so the two recovery paths
    stay symmetric.
    """
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_two_task_phase_plan())
    # Both tasks complete — the operator already cleared the infra
    # block and the worker landed it normally.
    for tid in ("1.1", "1.2"):
        await _walk_to_complete(pm, tid)
    # Phase was previously parked at paused while 1.1 was still infra-
    # blocked; now we simulate the post-resolution state.
    await pm.update_phase_meta("1", review_status="paused")

    orch = _make_orch(tmp_path)

    review_calls: list[str] = []

    async def _fake_run_phase_review(
        orch_arg: Any, phase: Phase
    ) -> None:
        review_calls.append(phase.id)
        await orch_arg.plan_manager.update_phase_meta(
            phase.id, review_status="accepted"
        )

    monkeypatch.setattr(ep, "_run_phase_review", _fake_run_phase_review)

    await orch.resume()

    plan = await orch.plan_manager.load()
    assert plan is not None
    phase = plan.phases[0]
    assert review_calls == ["1"]
    assert phase.review_status == "accepted"
