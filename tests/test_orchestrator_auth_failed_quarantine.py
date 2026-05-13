"""Tests for v0.29.0 Bug 7: :class:`AuthenticationFailedError` now stamps
the offending task as ``quarantined`` (not ``blocked``) and the resume
path picks it up automatically.

Supersedes the v0.28.0 contract pinned by
``test_orchestrator_auth_failed_halt.py`` — that test has been updated
in lockstep so the suite as a whole reflects the v0.29.0 behaviour.

Two scenarios:

  1. Single auth failure during a phase → task ends up ``quarantined``,
     phase ``review_status="paused"``, the typed exception re-raises so
     the CLI driver returns a non-zero exit code.
  2. After the operator clears the underlying issue, ``Orchestrator
     .resume`` re-dispatches the quarantined task, drives it to
     completion, and re-fires the phase-review tournament so the
     phase lands at ``review_status="accepted"``.
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
from tournament.errors import AuthenticationFailedError

from stub_adapter import StubAdapter, ok


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_single_phase_plan() -> Plan:
    return Plan(
        plan_id="p-auth-q",
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
        session_id="sess-test-auth-q",
    )


@pytest.mark.asyncio
async def test_auth_failed_marks_task_quarantined_not_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replaces the v0.28.0 contract that asserted ``blocked``.

    Seed the developer adapter to raise :class:`AuthenticationFailedError`.
    Assert:

      * :func:`run_execute_phase` re-raises (CLI exits non-zero).
      * The in-flight task ends up ``status="quarantined"`` (NOT
        ``blocked``) with ``blocked_reason`` carrying the typed
        ``auth_failed:`` prefix retained for forensics.
      * The phase's ``review_status`` is parked at ``"paused"`` (NOT
        ``None``, ``"accepted"`` or ``"skipped"``) so the resume path
        can re-trigger the review once the quarantined task resolves.
    """
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_single_phase_plan())
    orch = _make_orch(tmp_path)

    async def _raise_auth(
        orch_arg: Any, task: Task, worktree_mgr: Any = None
    ) -> Task:
        await orch_arg.plan_manager.update_task_status(task.id, "in_progress")
        raise AuthenticationFailedError(
            "auth_failed for role=developer: API Error: 403"
        )

    monkeypatch.setattr(ep, "_execute_one", _raise_auth)

    with pytest.raises(AuthenticationFailedError):
        await ep.run_execute_phase(orch)

    plan = await orch.plan_manager.load()
    assert plan is not None
    phase = plan.phases[0]
    task = phase.tasks[0]

    # v0.29.0 Bug 7: quarantined (was ``blocked`` in v0.28.0).
    assert task.status == "quarantined"
    assert task.blocked_reason is not None
    assert task.blocked_reason.startswith("auth_failed:")

    # v0.29.0 Bug 7: phase parked at paused — the resume path re-fires
    # phase-review explicitly. Must NOT be ``accepted`` or ``skipped``
    # (force-accept on a halt path is the production stall this fix
    # exists to prevent) and must NOT stay ``None`` (otherwise the
    # next phase aggregator pass would re-evaluate).
    assert phase.review_status == "paused"


@pytest.mark.asyncio
async def test_after_quarantine_resume_runs_to_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full integration: auth fails → task quarantined → operator
    "fixes" auth (we monkeypatch the adapter to succeed) → ``resume()``
    → task completes → phase review fires and accepts.

    The phase-review runner is monkeypatched to a no-op accept so the
    test doesn't depend on the full tournament infrastructure; we
    only care about the resume routing and ``paused`` clearing here.
    """
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_single_phase_plan())
    orch = _make_orch(tmp_path)

    # First pass: auth fails.
    async def _raise_auth(
        orch_arg: Any, task: Task, worktree_mgr: Any = None
    ) -> Task:
        await orch_arg.plan_manager.update_task_status(task.id, "in_progress")
        raise AuthenticationFailedError(
            "auth_failed for role=developer: API Error: 403"
        )

    monkeypatch.setattr(ep, "_execute_one", _raise_auth)

    with pytest.raises(AuthenticationFailedError):
        await ep.run_execute_phase(orch)

    # Confirm we're parked at quarantined / paused (the contract under
    # test in the previous case — tested again here as a precondition
    # for the resume scenario).
    plan = await orch.plan_manager.load()
    assert plan is not None
    assert plan.phases[0].tasks[0].status == "quarantined"
    assert plan.phases[0].review_status == "paused"

    # Second pass: operator "fixes" auth — adapter now succeeds and
    # walks the FSM to complete. Phase review is monkeypatched to a
    # no-op accept so we don't need the real tournament wiring here.
    seen_dispatch: list[str] = []

    async def _success_execute(
        orch_arg: Any, task: Task, worktree_mgr: Any = None
    ) -> Task:
        seen_dispatch.append(task.id)
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

    monkeypatch.setattr(ep, "_execute_one", _success_execute)

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
    task = phase.tasks[0]
    # Quarantined task was re-dispatched exactly once.
    assert seen_dispatch == ["1.1"]
    assert task.status == "complete"
    # Phase review fired and accepted.
    assert review_calls == ["1"]
    assert phase.review_status == "accepted"
