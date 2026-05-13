"""Tests for v0.29.0 Bug 7: ``quarantined`` :class:`TaskStatus` + ``paused``
:class:`Phase` review_status.

The ``quarantined`` status is a non-terminal halt state stamped by the
typed-infrastructure-failure path (e.g.
:class:`AuthenticationFailedError`). Unlike ``blocked`` it stays in the
non-terminal set used by :func:`orchestrator._find_in_progress_task`,
which means :meth:`Orchestrator.resume` picks quarantined tasks up
automatically once the operator clears the underlying issue.

The companion ``Phase.review_status="paused"`` value is set by the
phase aggregator when it observes a quarantined task in the phase. The
phase-review tournament is NOT fired (force-accepting on a halt path
is the production stall this fix exists to prevent); ``Orchestrator
.resume`` clears the paused state once the quarantined work resolves
so the tournament re-fires fresh.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator, _find_in_progress_task
from orchestrator import execute_phase as ep
from state.plan_manager import PlanManager
from state.schemas import AcceptanceCriterion, Phase, Plan, Task

from stub_adapter import StubAdapter, ok


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_two_task_phase_plan() -> Plan:
    """One phase, two tasks — used by the paused-phase fixtures so we can
    leave one task ``complete`` and put the other into ``quarantined``."""
    return Plan(
        plan_id="p-quarantine",
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
        session_id="sess-test-quarantine",
    )


@pytest.mark.asyncio
async def test_quarantined_is_non_terminal(tmp_path: Path) -> None:
    """``_find_in_progress_task`` returns a quarantined task (proving
    :meth:`Orchestrator.resume` will pick it up automatically).

    This is the key non-terminal contract: quarantined must NOT be
    treated like blocked or skipped (which the resume scan steps
    over) — otherwise the halt is unrecoverable without operator
    surgery.
    """
    pm = PlanManager(tmp_path, session_id="sess-init")
    plan = await pm.init_plan(_mk_two_task_phase_plan())

    # Walk task 1.1 through pending -> in_progress -> quarantined.
    await pm.update_task_status("1.1", "in_progress")
    await pm.update_task_status(
        "1.1",
        "quarantined",
        meta={"blocked_reason": "auth_failed: API Error: 403"},
    )

    plan = await pm.load()
    assert plan is not None
    found = _find_in_progress_task(plan)
    assert found is not None
    assert found.id == "1.1"
    assert found.status == "quarantined"
    # Forensic trail preserved.
    assert found.blocked_reason == "auth_failed: API Error: 403"


@pytest.mark.asyncio
async def test_resume_picks_up_quarantined_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seed a plan whose only non-terminal task is quarantined. Run
    :meth:`Orchestrator.resume` and assert the task transitions back
    through ``in_progress`` (the documented resume edge).

    ``_execute_one`` is monkeypatched to a no-op marker so the test
    doesn't depend on the full coder/QA/review pipeline; we only care
    about the resume routing here.
    """
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_two_task_phase_plan())
    # Mark 1.2 as complete so only 1.1 is non-terminal.
    for s in (
        "in_progress",
        "coded",
        "auto_gated",
        "reviewed",
        "tested",
        "tournamented",
        "complete",
    ):
        await pm.update_task_status("1.2", s)
    # Walk 1.1 to quarantined.
    await pm.update_task_status("1.1", "in_progress")
    await pm.update_task_status(
        "1.1",
        "quarantined",
        meta={"blocked_reason": "auth_failed: simulated"},
    )

    orch = _make_orch(tmp_path)

    seen_dispatch: list[str] = []

    async def _fake_execute_one(
        orch_arg: Any, task: Task, worktree_mgr: Any = None
    ) -> Task:
        # Walk the full FSM so the test confirms the resume edge fired.
        seen_dispatch.append(task.id)
        # The resume path has already parked the task at in_progress.
        # Drive it to complete via the canonical pipeline so the rest
        # of the orchestrator loop sees a terminal landing.
        for s in (
            "coded",
            "auto_gated",
            "reviewed",
            "tested",
            "tournamented",
            "complete",
        ):
            await orch_arg.plan_manager.update_task_status(task.id, s)
        return await orch_arg.plan_manager.get_task(task.id)

    monkeypatch.setattr(ep, "_execute_one", _fake_execute_one)

    await orch.resume()

    plan = await orch.plan_manager.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    assert task.id == "1.1"
    # The resume path must have dispatched the quarantined task
    # exactly once via the in_progress -> coded -> ... edge.
    assert seen_dispatch == ["1.1"]
    assert task.status == "complete"


@pytest.mark.asyncio
async def test_phase_with_quarantined_task_does_not_auto_accept(
    tmp_path: Path,
) -> None:
    """Quarantined task in phase, all other tasks complete →
    ``_maybe_run_phase_review`` parks the phase at
    ``review_status="paused"``, NOT ``"accepted"``.

    This is the production stall guard: force-accepting a partial
    phase on the halt path is the v0.28.0-and-prior failure mode that
    Bug 7 closes.
    """
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_two_task_phase_plan())
    # Walk 1.2 fully to complete.
    for s in (
        "in_progress",
        "coded",
        "auto_gated",
        "reviewed",
        "tested",
        "tournamented",
        "complete",
    ):
        await pm.update_task_status("1.2", s)
    # Walk 1.1 to quarantined.
    await pm.update_task_status("1.1", "in_progress")
    await pm.update_task_status(
        "1.1",
        "quarantined",
        meta={"blocked_reason": "auth_failed: simulated"},
    )

    orch = _make_orch(tmp_path)

    await ep._maybe_run_phase_review(orch, "1")

    plan = await orch.plan_manager.load()
    assert plan is not None
    phase = plan.phases[0]
    assert phase.review_status == "paused"
    # And the quarantined task is unchanged.
    assert phase.tasks[0].status == "quarantined"


@pytest.mark.asyncio
async def test_phase_paused_re_triggers_review_after_quarantine_resolved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seed a phase parked at ``review_status="paused"`` with the
    quarantined task already resolved (e.g. operator ran ``autodev
    requeue`` then the task completed). :meth:`Orchestrator.resume`
    must clear the paused state and re-trigger the phase-review
    tournament.

    The phase-review tournament itself is monkeypatched to a no-op
    that simply stamps the phase ``"accepted"`` so we can observe
    that the resume path drove the review through to conclusion.
    """
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_two_task_phase_plan())
    # Walk both tasks to complete.
    for tid in ("1.1", "1.2"):
        for s in (
            "in_progress",
            "coded",
            "auto_gated",
            "reviewed",
            "tested",
            "tournamented",
            "complete",
        ):
            await pm.update_task_status(tid, s)
    # Park the phase at paused (mirrors what the aggregator did
    # before the quarantined task was resolved).
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
    # Resume cleared paused, then re-triggered review which accepted.
    assert review_calls == ["1"]
    assert phase.review_status == "accepted"
