"""Integration tests for v0.30.0 Bug 5: orchestrator wiring of the
:class:`InfraFailureCircuitBreaker`.

Covers:

* The breaker, when it trips on a series of infrastructure failures
  fed through ``delegate()``, halts the run with the same quarantine +
  paused-phase + non-zero exit pattern as the v0.28.0/v0.29.0
  ``AuthenticationFailedError`` halt.
* The trip threshold is configurable via
  ``cfg.circuit_breaker_threshold`` (and the breaker honours it when
  the orchestrator wires it).
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
from orchestrator.circuit_breaker import InfraFailureCircuitBreaker
from state.plan_manager import PlanManager
from state.schemas import AcceptanceCriterion, Phase, Plan, Task
from tournament.errors import InfrastructureCircuitOpenError

from stub_adapter import StubAdapter, ok


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_single_phase_plan() -> Plan:
    return Plan(
        plan_id="p-cb",
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
        session_id="sess-test-cb",
    )


@pytest.mark.asyncio
async def test_circuit_open_halts_run_with_actionable_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pre-pop the breaker's counter so the next failure trips it inside
    the worker, then drive the execute loop and assert:

      * :func:`run_execute_phase` re-raises
        :class:`InfrastructureCircuitOpenError`.
      * The in-flight task is stamped ``quarantined`` (NOT ``blocked``).
      * ``blocked_reason`` carries the ``infra_circuit_open:`` prefix
        so post-mortems can grep for the typed halt.
      * The phase's ``review_status`` is parked at ``"paused"``.
      * The console message names the failure mode and tells the
        operator to refresh credentials and ``autodev resume``.
    """
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_single_phase_plan())
    orch = _make_orch(tmp_path)

    # Drive the breaker to ``threshold-1`` failures first via direct
    # API — the next failure (raised by the worker below) is the one
    # that trips and surfaces the typed exception.
    fixed_now = _dt.datetime(2026, 5, 13, 12, 0, 0, tzinfo=_dt.timezone.utc)
    orch._circuit_breaker.record_failure("seed-t-a", "auth_failed", fixed_now)
    orch._circuit_breaker.record_failure("seed-t-b", "server_error", fixed_now)
    halt, _ = orch._circuit_breaker.should_halt()
    assert halt is False  # precondition: 2 of 3 — closed

    async def _raise_circuit(
        orch_arg: Any, task: Task, worktree_mgr: Any = None
    ) -> Task:
        # Mimic delegate() raising the typed exception once the breaker
        # trips. The worker's typed-catch then quarantines the task.
        await orch_arg.plan_manager.update_task_status(task.id, "in_progress")
        # Bump the breaker via its public API so the production path
        # would have raised; the test then raises the typed exception
        # to mirror the delegate() raise site.
        orch_arg._circuit_breaker.record_failure(
            task.id,
            "auth_failed",
            _dt.datetime(2026, 5, 13, 12, 0, 5, tzinfo=_dt.timezone.utc),
        )
        _h, reason = orch_arg._circuit_breaker.should_halt()
        assert _h is True  # precondition: 3rd failure trips
        raise InfrastructureCircuitOpenError(reason or "circuit open")

    monkeypatch.setattr(ep, "_execute_one", _raise_circuit)

    with pytest.raises(InfrastructureCircuitOpenError):
        await ep.run_execute_phase(orch)

    plan = await orch.plan_manager.load()
    assert plan is not None
    phase = plan.phases[0]
    task = phase.tasks[0]

    assert task.status == "quarantined"
    assert task.blocked_reason is not None
    assert task.blocked_reason.startswith("infra_circuit_open:")
    # The reason text must mention the concrete count and window so
    # post-mortems can correlate the halt with the upstream incident.
    assert "3" in task.blocked_reason
    assert "60" in task.blocked_reason

    # Phase parked at paused — the resume path will re-fire phase-
    # review explicitly. Must NOT be ``accepted`` (force-accept on a
    # halt path is the production stall this fix exists to prevent)
    # and must NOT stay ``None`` (otherwise the next phase aggregator
    # pass would re-evaluate).
    assert phase.review_status == "paused"

    # Operator-facing console message names the failure mode and
    # carries the actionable next step.
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "infrastructure circuit open" in combined
    assert "autodev resume" in combined


@pytest.mark.asyncio
async def test_circuit_breaker_threshold_configurable_via_config(
    tmp_path: Path,
) -> None:
    """Setting ``cfg.circuit_breaker_threshold=5`` produces an
    orchestrator whose breaker takes 5 failures (not the default 3)
    to trip. Verifies the cfg → orchestrator wiring without driving
    the full run loop.
    """
    cfg = default_config()
    cfg.tournaments.auto_disable_for_models = []
    cfg.tournaments.impl.enabled = False
    cfg.tournaments.plan.enabled = False
    cfg.circuit_breaker_threshold = 5
    cfg.circuit_breaker_window_s = 120.0
    registry = build_registry(cfg)
    adapter = StubAdapter({"explorer": ok("ok")})
    orch = Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-test-cb-cfg",
    )

    # Sanity: the orchestrator built a real breaker with the configured
    # knobs (not the defaults).
    assert isinstance(orch._circuit_breaker, InfraFailureCircuitBreaker)
    assert orch._circuit_breaker.threshold == 5
    assert orch._circuit_breaker.window_s == 120.0

    # Four failures must NOT trip (default would have at 3) — proves
    # the larger threshold flowed through.
    base = _dt.datetime(2026, 5, 13, 12, 0, 0, tzinfo=_dt.timezone.utc)
    for i in range(4):
        orch._circuit_breaker.record_failure(
            f"t-{i}", "server_error", base + _dt.timedelta(seconds=i)
        )
    halt, _ = orch._circuit_breaker.should_halt()
    assert halt is False

    # Fifth failure trips.
    orch._circuit_breaker.record_failure(
        "t-5", "server_error", base + _dt.timedelta(seconds=5)
    )
    halt, reason = orch._circuit_breaker.should_halt()
    assert halt is True
    assert reason is not None
    assert "5" in reason
    assert "120" in reason
