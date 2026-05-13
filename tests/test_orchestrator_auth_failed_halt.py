"""Tests for v0.28.0 Bug 2: orchestrator halts cleanly on
:class:`AuthenticationFailedError`.

When the tournament classifier raises :class:`AuthenticationFailedError`
(``subtype="auth_failed"`` from the upstream API), the orchestrator's
top-level loop in :func:`orchestrator.execute_phase.run_execute_phase`
must:

  1. Catch the typed exception.
  2. Mark the in-flight task as ``blocked`` with
     ``blocked_reason="auth_failed: <error>"``.
  3. Re-raise so the CLI surface returns a non-zero exit code.
  4. NOT mark the phase as ``accepted`` or ``skipped`` (the
     phase-review tournament must not fire on a halt path; that would
     force-accept an empty / partial phase, which is the production
     stall this fix exists to prevent).

Bug 7 in v0.29.0 will replace ``blocked`` with ``quarantined`` so the
halt becomes resumable; the test below pins the v0.28.0 contract.
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
    """One phase, one task — minimal fixture for the halt path."""
    return Plan(
        plan_id="p-auth-halt",
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
        session_id="sess-test-auth-halt",
    )


@pytest.mark.asyncio
async def test_auth_failed_during_phase_aborts_loop_without_force_accept(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seed the developer adapter to raise :class:`AuthenticationFailedError`
    during the task's first dispatch. Assert:

      * :func:`run_execute_phase` re-raises the typed exception (so the
        CLI driver returns a non-zero exit code);
      * the in-flight task ends up in ``status="blocked"`` with a
        ``blocked_reason`` carrying the ``auth_failed:`` prefix;
      * the phase's ``review_status`` stays ``None`` — NOT
        ``"accepted"`` or ``"skipped"``. (The phase-review tournament
        must not fire on a halt path; force-accepting a half-empty
        phase is the production stall Bug 2 exists to prevent.)
    """
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_single_phase_plan())
    orch = _make_orch(tmp_path)

    # Replace ``_execute_one`` with a fake that walks the FSM to
    # ``in_progress`` then raises :class:`AuthenticationFailedError` —
    # mirroring what the real ``delegate`` would do when the tournament
    # classifier short-circuits on ``subtype="auth_failed"``.
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

    # Task is blocked with the typed reason (Bug 7 in v0.29.0 will
    # change this to "quarantined").
    assert task.status == "blocked"
    assert task.blocked_reason is not None
    assert task.blocked_reason.startswith("auth_failed:")

    # Phase review NEVER fired — no force-accept on the halt path.
    assert phase.review_status is None
