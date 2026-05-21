"""v0.37.0 H3 integration: cross-task ``capture_failed`` test-diag circuit.

Pins the end-to-end behaviour added in
:meth:`orchestrator.circuit_breaker.InfraFailureCircuitBreaker.record_test_diagnosis`
plus the matching feed-and-trip site in
:func:`orchestrator.execute_phase._execute_one`.

Scenario (motivating real-world failure pattern):

  Multiple distinct tasks each produce a single
  :class:`~orchestrator.test_result_classifier.TestDiagnosis` of
  ``capture_failed`` (empty stdout, null returncode, empty stderr).
  Before H3 each retried once and hard-failed in isolation while the
  surrounding plan kept generating tasks — no cross-task signal ever
  halted the run. After H3 the third occurrence across distinct tasks
  trips the breaker, the in-flight task is quarantined, and the phase
  is parked at ``review_status="paused"`` so the operator can
  ``autodev doctor`` the runner before resuming.

Drives the stub test_engineer adapter directly because the
``fake-pytest`` ``capture_failed`` mode is a subprocess fixture and
this test stays in-process; the diagnosis classification is identical
either way (``classify_test_result`` resolves to ``capture_failed`` on
``success=False`` AND empty ``text`` AND empty ``raw_stderr``).
"""

from __future__ import annotations

import datetime as _dt
import time as _time
from pathlib import Path

import pytest

from adapters.types import AgentResult
from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator import execute_phase as ep
from state.schemas import (
    AcceptanceCriterion,
    Phase,
    Plan,
    Task,
)
from tournament.errors import InfrastructureCircuitOpenError

from stub_adapter import StubAdapter, ok


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_multi_task_plan() -> Plan:
    """Single phase with four tasks. The breaker should trip on the
    third capture_failed before the fourth task is reached.
    """
    tasks = [
        Task(
            id=f"1.{i}",
            phase_id="1",
            title=f"Task {i}",
            description=f"Implement op_{i}",
            files=[f"mod_{i}.py"],
            acceptance=[
                AcceptanceCriterion(id=f"ac-{i}", description="tests pass")
            ],
        )
        for i in range(1, 5)
    ]
    return Plan(
        plan_id="p-capture-failed",
        spec_hash="abcdef1234567890",
        phases=[Phase(id="1", title="Implement", tasks=tasks)],
        created_at=_iso(),
        updated_at=_iso(),
    )


def _coder_ok(variant: str) -> AgentResult:
    """Stub developer return — diff is perturbed per-variant so the loop
    detector (which hashes adapter output) doesn't trip across the
    multi-task sequence."""
    return AgentResult(
        success=True,
        text=f"wrote op ({variant})",
        diff=(
            "diff --git a/mod.py b/mod.py\n"
            "--- a/mod.py\n"
            "+++ b/mod.py\n"
            "@@ -0,0 +1 @@\n"
            f"+def op_{variant}(x): return x  # {variant}\n"
        ),
        files_changed=[Path(f"mod_{variant}.py")],
        duration_s=0.01,
    )


def _capture_failed_result() -> AgentResult:
    """Empty text AND empty stderr AND non-success → classifier
    diagnoses ``capture_failed`` (see
    :func:`orchestrator.test_result_classifier.classify_test_result`
    branch 5).
    """
    return AgentResult(
        success=False,
        text="",
        raw_stderr="",
        error=None,
        duration_s=0.01,
    )


async def _make_orch(cwd: Path, adapter: StubAdapter) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    # Default H3 knobs (threshold=3, window=600s) — explicit for clarity.
    cfg.test_diag_breaker_threshold = 3
    cfg.test_diag_breaker_window_s = 600.0
    cfg.test_diag_breaker_diagnoses = ["capture_failed"]
    # v0.38.0 I4: fast backoff knobs so the integration test trips on
    # budget exhaustion in well under 1s of real sleep time. Production
    # defaults (initial=5s, budget=600s) would make this test take
    # ~10 minutes; the contract under test is the same.
    cfg.test_diag_backoff_initial_s = 0.05
    cfg.test_diag_backoff_multiplier = 2.0
    cfg.test_diag_backoff_max_s = 0.5
    cfg.test_diag_backoff_total_budget_s = 0.4
    # Also clamp the parallel-pool drain so the HK6 fast-teardown
    # contract is exercised by this integration test.
    cfg.parallel_pool_drain_timeout_s = 1.0
    registry = build_registry(cfg)
    orch = Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-v037-h3",
    )
    await orch.plan_manager.init_plan(_mk_multi_task_plan())
    return orch


@pytest.mark.asyncio
async def test_third_capture_failed_trips_circuit_and_pauses_phase(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Three ``capture_failed`` diagnoses across distinct tasks trip the
    breaker; the run halts, the in-flight task is quarantined, the phase
    is parked at ``review_status="paused"``, and the structured trip
    log line is emitted.

    Sequence under default knobs (threshold=3, window=600s): three
    ``capture_failed`` events across the multi-task plan trip the
    breaker. The orchestrator's batched scheduling means the exact
    task that fires the trip is an implementation detail; what's
    pinned here is that the trip raises, the trip log line emits,
    the phase ends paused, and at least one task in the plan never
    started.
    """
    adapter = StubAdapter(
        {
            # Developer always succeeds — the test-engineer leg is the
            # one we drive. Different variants per call avoid the loop
            # detector tripping.
            "developer": [_coder_ok(str(i)) for i in range(1, 10)],
            # Reviewer never reached on the capture_failed path because
            # the test-runner verdict is processed first; default ok
            # behaviour is fine as a safety net.
            "reviewer": ok("APPROVED\n- clean"),
            # Six capture_failed results queued — enough for two
            # per-task attempts on tasks 1 and 2 plus headroom; the
            # breaker trips inside task 2's first attempt.
            "test_engineer": [_capture_failed_result() for _ in range(6)],
        }
    )
    orch = await _make_orch(tmp_path, adapter)

    # v0.38.0 I4 (HK6): elapsed-time assertion. Pre-I4 the unbounded
    # ``asyncio.gather`` drain stalled the process for ~30s post-trip.
    # With the bounded drain + fast budget knobs the run should
    # complete within a few seconds.
    _t_start = _time.monotonic()
    with pytest.raises(InfrastructureCircuitOpenError) as exc_info:
        await ep.run_execute_phase(orch)
    _elapsed = _time.monotonic() - _t_start
    assert _elapsed < 15.0, (
        f"v0.38.0 I4 HK6 regression: run took {_elapsed:.2f}s "
        f"(expected < 15s with bounded drain)"
    )

    # v0.38.0 I4: reason text now names the budget exhaustion path
    # (test-diagnosis + capture_failed + budget seconds).
    assert "test-diagnosis" in str(exc_info.value)
    assert "capture_failed" in str(exc_info.value)
    assert "backoff budget" in str(exc_info.value)

    # v0.38.0 I4 (HK7): the typed identifier is now carried on the
    # exception so the halt handler doesn't have to walk the plan.
    assert exc_info.value.halted_task_id is not None
    assert exc_info.value.halted_task_id.startswith("1.")

    # v0.38.0 I4: structured log line is now the budget-exhausted op.
    # (The pre-I4 ``execute_phase.test_diag_breaker_trip`` op still
    # fires on the adapter-class trip path, but the capture_failed-only
    # stream now flows through the new event name.)
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert (
        "execute_phase.test_diag_budget_exhausted" in combined
        or "execute_phase.test_diag_breaker_trip" in combined
    )
    # Backoff-iteration log lines should also fire at least once
    # before the budget-exhaustion raise.
    assert "execute_phase.test_diag_backoff" in combined

    plan = await orch.plan_manager.load()
    assert plan is not None
    phase = plan.phases[0]

    # Phase parked at paused — quarantined-task aggregator pause is the
    # same path the v0.30.0 adapter-class breaker uses.
    assert phase.review_status == "paused"

    # At least one task should be in a halt-terminal state — either
    # ``blocked`` (the hard-fail branch) or ``quarantined`` (the
    # typed-catch site, post-H3-allowed transition).
    statuses = [t.status for t in phase.tasks]
    assert any(s in ("quarantined", "blocked") for s in statuses), statuses

    # None of the tasks should be ``complete`` — the run halted before
    # any task drained the full pipeline. ``pending`` may or may not
    # be present depending on the scheduler's batching (parallel pool
    # can dispatch all queued tasks before the trip lands).
    assert not any(s == "complete" for s in statuses), statuses
    # And at least one task carries a ``capture_failed`` trace —
    # either in its ``blocked_reason`` (hard-fail branch) or via the
    # typed quarantine reason on the trip-target task.
    reasons = " ".join(
        (t.blocked_reason or "") for t in phase.tasks
    )
    assert "capture_failed" in reasons


@pytest.mark.asyncio
async def test_two_capture_failed_in_window_does_not_trip(
    tmp_path: Path,
) -> None:
    """Two ``capture_failed`` diagnoses (within window) must NOT trip —
    the existing per-task retry-then-hard-fail path runs and the task
    ends blocked, but the run loop continues to the next task.

    Same fixtures as the trip test but with threshold raised to 5 so
    the two ``capture_failed`` events from task 1 stay below the trip
    line and the breaker stays closed.
    """
    adapter = StubAdapter(
        {
            "developer": [_coder_ok(str(i)) for i in range(1, 10)],
            "reviewer": ok("APPROVED\n- clean"),
            "test_engineer": [_capture_failed_result() for _ in range(4)],
        }
    )

    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    cfg.test_diag_breaker_threshold = 5  # well above the 2 we'll feed
    registry = build_registry(cfg)
    orch = Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-v037-h3-no-trip",
    )
    plan_obj = _mk_multi_task_plan()
    # Trim to a single task so the run completes naturally after the
    # task hard-fails (no other tasks would push the count further).
    plan_obj.phases[0].tasks = plan_obj.phases[0].tasks[:1]
    await orch.plan_manager.init_plan(plan_obj)

    # Run should NOT raise InfrastructureCircuitOpenError — the per-
    # task hard-fail path handles the diagnosis without the cross-task
    # breaker tripping.
    await ep.run_execute_phase(orch)

    plan = await orch.plan_manager.load()
    assert plan is not None
    phase = plan.phases[0]
    # Task ended blocked via the existing v0.32.0 hard-fail branch.
    assert phase.tasks[0].status == "blocked"
    # Phase is NOT paused (no breaker trip).
    assert phase.review_status != "paused"


@pytest.mark.asyncio
async def test_i4_auto_reset_clears_failure_burst(tmp_path: Path) -> None:
    """v0.38.0 I4: ``record_test_success`` × N within the auto-reset
    window clears the test-diag failure deque so a healthy run after
    a flaky burst doesn't keep the circuit armed.

    This test drives :class:`InfraFailureCircuitBreaker` directly via
    the orchestrator's wired instance — the multi-task pipeline-level
    integration of the auto-reset is exercised by the breaker's unit
    tests; here we pin that the orchestrator's wiring exposes the
    new method and that successive successes restore a closed state.
    """
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    cfg.test_diag_breaker_threshold = 3
    cfg.test_diag_auto_reset_after_n_successes = 3
    cfg.test_diag_auto_reset_window_s = 900.0
    cfg.test_diag_backoff_initial_s = 0.01
    cfg.test_diag_backoff_total_budget_s = 100.0  # well above 0.01

    registry = build_registry(cfg)
    orch = Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=StubAdapter({}),
        registry=registry,
        session_id="sess-v038-i4-auto-reset",
    )

    cb = orch._circuit_breaker
    now = _dt.datetime.now(_dt.timezone.utc)
    cb.record_test_diagnosis("1.1", "capture_failed", now)
    cb.record_test_diagnosis(
        "1.2", "capture_failed", now + _dt.timedelta(seconds=10)
    )
    cb.record_test_diagnosis(
        "1.3", "capture_failed", now + _dt.timedelta(seconds=20)
    )
    # Threshold crossed → backoff returns.
    assert cb.next_backoff_s_for_test_diag() == 0.01

    # Three successes within window → clears.
    for j in range(3):
        cb.record_test_success(
            f"ok_{j}", now + _dt.timedelta(seconds=30 + j * 10)
        )
    # Closed again — no backoff.
    assert cb.next_backoff_s_for_test_diag() is None
