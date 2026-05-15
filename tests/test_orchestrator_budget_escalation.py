"""Tests for v0.31.0 Phase 3 — per-(task_id, role) budget escalation.

Two layers of coverage:

* ``escalate_budget`` and ``BudgetEscalationTracker`` — pure-function +
  pure-state helpers in :mod:`orchestrator.budget_escalation`. Cheap to
  cover exhaustively (every rung, every reset condition).
* :func:`orchestrator.execute_phase.delegate` — the integration site
  that consumes the tracker. Driven via ``StubAdapter`` to inject
  ``error_max_turns`` / other failure / success subtypes and observe
  the resulting ``AgentInvocation.max_turns`` / ``timeout_s``.

The integration tests deliberately run :func:`delegate` directly rather
than the full execute-phase loop — the helper is the right blast
radius (cheaper to test, smaller surface area for drift).
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
from orchestrator.budget_escalation import (
    DEFAULT_MAX_TURNS_CEILING,
    DEFAULT_TIMEOUT_S_CEILING,
    MAX_ESCALATIONS,
    BudgetEscalationTracker,
    escalate_budget,
)
from orchestrator.delegation_envelope import DelegationEnvelope
from state.plan_manager import PlanManager
from state.schemas import AcceptanceCriterion, Phase, Plan, Task

from stub_adapter import StubAdapter


# ---------------------------------------------------------------------------
# Pure escalate_budget()
# ---------------------------------------------------------------------------


def test_escalate_budget_first_attempt_is_noop() -> None:
    """``attempt=0`` returns the base budget unchanged."""
    new_max, new_timeout = escalate_budget(10, 600, 0)
    assert new_max == 10
    assert new_timeout == 600


def test_escalate_budget_second_attempt_uses_one_and_a_half_curve() -> None:
    """``attempt=1`` applies 1.5× turns and +25% timeout."""
    new_max, new_timeout = escalate_budget(10, 600, 1)
    # 10 * 1.5 = 15
    assert new_max == 15
    # 600 * 1.25 = 750
    assert new_timeout == 750


def test_escalate_budget_third_attempt_uses_two_x_curve() -> None:
    """``attempt=2`` applies 2.0× turns and +50% timeout."""
    new_max, new_timeout = escalate_budget(10, 600, 2)
    assert new_max == 20
    assert new_timeout == 900


def test_escalate_budget_rounds_up_with_ceil() -> None:
    """Fractional bumps round UP (3 turns → 5, not 4)."""
    new_max, _ = escalate_budget(3, 100, 1)
    # ceil(3 * 1.5) = ceil(4.5) = 5
    assert new_max == 5


def test_escalate_budget_preserves_none_timeout() -> None:
    """``base_timeout_s=None`` propagates through escalation."""
    new_max, new_timeout = escalate_budget(10, None, 1)
    assert new_max == 15
    assert new_timeout is None


def test_escalate_budget_respects_ceiling() -> None:
    """Both ceilings cap the bumped values, even when the curve would exceed."""
    new_max, new_timeout = escalate_budget(
        100, 3000, 2,
        max_turns_ceiling=50,
        timeout_s_ceiling=2000,
    )
    # 100 * 2 = 200, capped to 50.
    assert new_max == 50
    # 3000 * 1.5 = 4500, capped to 2000.
    assert new_timeout == 2000


def test_escalate_budget_attempt_clamped_above_ladder() -> None:
    """``attempt=99`` clamps to the last curve entry rather than crashing."""
    new_max, new_timeout = escalate_budget(10, 600, 99)
    # Same as attempt=2 (the highest defined curve).
    assert new_max == 20
    assert new_timeout == 900


# ---------------------------------------------------------------------------
# BudgetEscalationTracker state machine
# ---------------------------------------------------------------------------


def test_tracker_starts_at_zero_for_unseen_pair() -> None:
    """A fresh ``(task_id, role)`` pair returns ``current_attempt=0``."""
    tr = BudgetEscalationTracker()
    assert tr.current_attempt("1.1", "developer") == 0
    assert tr.is_exhausted("1.1", "developer") is False


def test_tracker_increments_on_error_max_turns() -> None:
    """Each ``error_max_turns`` bumps the counter for that pair."""
    tr = BudgetEscalationTracker()
    tr.record_result("1.1", "developer", "error_max_turns")
    assert tr.current_attempt("1.1", "developer") == 1
    tr.record_result("1.1", "developer", "error_max_turns")
    assert tr.current_attempt("1.1", "developer") == 2


def test_tracker_resets_on_success() -> None:
    """A successful subtype clears the counter (any non-max-turns subtype)."""
    tr = BudgetEscalationTracker()
    tr.record_result("1.1", "developer", "error_max_turns")
    tr.record_result("1.1", "developer", "success")
    assert tr.current_attempt("1.1", "developer") == 0


def test_tracker_resets_on_other_failure_subtypes() -> None:
    """``timeout`` / ``parse_error`` / ``rate_limited`` all reset the counter."""
    for other_subtype in ("timeout", "parse_error", "rate_limited", "auth_failed"):
        tr = BudgetEscalationTracker()
        tr.record_result("1.1", "developer", "error_max_turns")
        tr.record_result("1.1", "developer", other_subtype)
        assert tr.current_attempt("1.1", "developer") == 0, (
            f"subtype={other_subtype!r} should have reset the counter"
        )


def test_tracker_pair_isolation_per_task() -> None:
    """Bumping ``(1.1, developer)`` doesn't affect ``(1.2, developer)``."""
    tr = BudgetEscalationTracker()
    tr.record_result("1.1", "developer", "error_max_turns")
    tr.record_result("1.1", "developer", "error_max_turns")
    assert tr.current_attempt("1.1", "developer") == 2
    assert tr.current_attempt("1.2", "developer") == 0


def test_tracker_pair_isolation_per_role() -> None:
    """Bumping ``(1.1, developer)`` doesn't affect ``(1.1, reviewer)``."""
    tr = BudgetEscalationTracker()
    tr.record_result("1.1", "developer", "error_max_turns")
    tr.record_result("1.1", "developer", "error_max_turns")
    assert tr.current_attempt("1.1", "developer") == 2
    assert tr.current_attempt("1.1", "reviewer") == 0


def test_tracker_is_exhausted_after_max_escalations() -> None:
    """After ``MAX_ESCALATIONS`` consecutive max-turns, ``is_exhausted`` flips."""
    tr = BudgetEscalationTracker()
    # MAX_ESCALATIONS is the number of *bumps* (defaults to 2). So three
    # consecutive max-turns gets us to count=3, which exceeds the ladder
    # depth (2) and is exhausted.
    for _ in range(MAX_ESCALATIONS + 1):
        tr.record_result("1.1", "developer", "error_max_turns")
    assert tr.is_exhausted("1.1", "developer") is True


# ---------------------------------------------------------------------------
# Integration: delegate() consumes the tracker
# ---------------------------------------------------------------------------


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_single_task_plan(task_id: str = "1.1") -> Plan:
    return Plan(
        plan_id="p-budget-escalate",
        spec_hash="0123456789abcdef",
        phases=[
            Phase(
                id="1",
                title="Setup",
                tasks=[
                    Task(
                        id=task_id,
                        phase_id="1",
                        title="t1",
                        description="d1",
                        complexity="simple",
                    ),
                ],
                acceptance=[AcceptanceCriterion(id="ph-1", description="ok")],
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
        complexity="simple",
    )


def _mk_developer_envelope(task_id: str = "1.1") -> DelegationEnvelope:
    return DelegationEnvelope(
        task_id=task_id,
        target_agent="developer",
        action="implement",
    )


def _mk_developer_task(task_id: str = "1.1") -> Task:
    return Task(
        id=task_id,
        phase_id="1",
        title="t",
        description="d",
        complexity="simple",
    )


def _make_orch(cwd: Path, adapter: StubAdapter) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    cfg.tournaments.phase_review.enabled = False
    registry = build_registry(cfg)
    return Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-budget-escalate",
    )


async def _seed_plan(cwd: Path, plan: Plan) -> None:
    pm = PlanManager(cwd, session_id="sess-init")
    await pm.init_plan(plan)


def _max_turns_failure(text: str = "") -> Any:
    """Build a fake adapter result with ``subtype=error_max_turns``."""
    from adapters.types import AgentResult

    return AgentResult(
        success=False,
        text=text,
        duration_s=0.01,
        error="hit max turns",
        subtype="error_max_turns",
    )


def _success_result(text: str = "ok") -> Any:
    from adapters.types import AgentResult

    return AgentResult(
        success=True,
        text=text,
        duration_s=0.01,
        subtype="success",
    )


def _other_failure(subtype: str) -> Any:
    from adapters.types import AgentResult

    return AgentResult(
        success=False,
        text="",
        duration_s=0.01,
        error=f"{subtype} happened",
        subtype=subtype,
    )


@pytest.mark.asyncio
async def test_escalates_on_repeated_max_turns(tmp_path: Path) -> None:
    """Three consecutive max-turns adapter results: 2nd call uses 1.5×,
    3rd call uses 2.0× of the base ``max_turns``."""
    await _seed_plan(tmp_path, _mk_single_task_plan())
    # Adapter returns max_turns failure on every call so the tracker keeps
    # incrementing across the three delegate() calls below.
    adapter = StubAdapter(
        {"developer": [_max_turns_failure(), _max_turns_failure(), _max_turns_failure()]}
    )
    orch = _make_orch(tmp_path, adapter)
    env = _mk_developer_envelope()
    task = _mk_developer_task()

    # First call: no escalation — uses the base.
    await ep.delegate(orch, "developer", env, task=task)
    base_max_turns = adapter.calls[0].max_turns
    base_timeout_s = adapter.calls[0].timeout_s
    assert base_max_turns >= 1
    # ``simple`` complexity yields 10 turns / 600s timeout from
    # tournament.task_overrides; assert by computation rather than literal
    # so a future bump to the per-bucket defaults doesn't accidentally
    # break this test.
    expected_second_max = int(round(base_max_turns * 1.5 + 0.0001))
    if base_max_turns * 1.5 != expected_second_max:
        # ceil semantics
        expected_second_max = int(base_max_turns * 1.5) + 1

    # Second call: 1.5× turns, +25% timeout.
    await ep.delegate(orch, "developer", env, task=task)
    second_max_turns = adapter.calls[1].max_turns
    second_timeout_s = adapter.calls[1].timeout_s
    # Use ceiling (matches escalate_budget's math.ceil).
    import math

    assert second_max_turns == math.ceil(base_max_turns * 1.5)
    if base_timeout_s is not None:
        assert second_timeout_s == math.ceil(base_timeout_s * 1.25)

    # Third call: 2.0× turns, +50% timeout (relative to the BASE).
    await ep.delegate(orch, "developer", env, task=task)
    third_max_turns = adapter.calls[2].max_turns
    third_timeout_s = adapter.calls[2].timeout_s
    assert third_max_turns == math.ceil(base_max_turns * 2.0)
    if base_timeout_s is not None:
        assert third_timeout_s == math.ceil(base_timeout_s * 1.5)


@pytest.mark.asyncio
async def test_does_not_escalate_on_other_failures(tmp_path: Path) -> None:
    """``timeout`` / ``parse_error`` / ``rate_limited`` results MUST NOT
    increment the escalation counter."""
    await _seed_plan(tmp_path, _mk_single_task_plan())
    adapter = StubAdapter(
        {
            "developer": [
                _other_failure("timeout"),
                _other_failure("parse_error"),
                _other_failure("rate_limited"),
            ]
        }
    )
    orch = _make_orch(tmp_path, adapter)
    env = _mk_developer_envelope()
    task = _mk_developer_task()

    await ep.delegate(orch, "developer", env, task=task)
    base_max_turns = adapter.calls[0].max_turns

    # Two more failures with different subtypes — neither should bump.
    await ep.delegate(orch, "developer", env, task=task)
    await ep.delegate(orch, "developer", env, task=task)

    # All three calls should have used the same base max_turns.
    assert adapter.calls[1].max_turns == base_max_turns
    assert adapter.calls[2].max_turns == base_max_turns


@pytest.mark.asyncio
async def test_escalation_caps_after_three_bumps(tmp_path: Path) -> None:
    """A 4th consecutive max-turns hard-fails with the exhaustion diagnostic
    rather than dispatching another adapter call."""
    await _seed_plan(tmp_path, _mk_single_task_plan())
    # Adapter is configured to ALWAYS return max_turns; we expect the 4th
    # delegate() call to short-circuit BEFORE invoking the adapter.
    adapter = StubAdapter(
        {
            "developer": [
                _max_turns_failure(),
                _max_turns_failure(),
                _max_turns_failure(),
                _max_turns_failure(),
            ]
        }
    )
    orch = _make_orch(tmp_path, adapter)
    env = _mk_developer_envelope()
    task = _mk_developer_task()

    # Three real dispatches (base, 1.5×, 2.0×).
    await ep.delegate(orch, "developer", env, task=task)
    await ep.delegate(orch, "developer", env, task=task)
    await ep.delegate(orch, "developer", env, task=task)
    assert adapter.count("developer") == 3

    # 4th delegate() short-circuits with the diagnostic — adapter is NOT
    # called a 4th time.
    result = await ep.delegate(orch, "developer", env, task=task)
    assert adapter.count("developer") == 3, (
        "delegate() should NOT have invoked the adapter on the exhausted attempt"
    )
    assert result.success is False
    assert result.subtype == "error_max_turns_escalation_exhausted"
    assert "budget escalation exhausted" in (result.error or "")


@pytest.mark.asyncio
async def test_escalation_resets_per_task(tmp_path: Path) -> None:
    """Escalation on task 1.1 does NOT bleed into task 1.2."""
    plan = Plan(
        plan_id="p-multi-task",
        spec_hash="0123456789abcdef",
        phases=[
            Phase(
                id="1",
                title="Setup",
                tasks=[
                    Task(id="1.1", phase_id="1", title="t1", description="d", complexity="simple"),
                    Task(id="1.2", phase_id="1", title="t2", description="d", complexity="simple"),
                ],
                acceptance=[AcceptanceCriterion(id="ph-1", description="ok")],
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
        complexity="simple",
    )
    await _seed_plan(tmp_path, plan)
    adapter = StubAdapter(
        {"developer": [_max_turns_failure(), _max_turns_failure(), _max_turns_failure()]}
    )
    orch = _make_orch(tmp_path, adapter)
    task1 = _mk_developer_task("1.1")
    task2 = _mk_developer_task("1.2")

    # Two calls on task 1.1 → tracker count = 2 for (1.1, developer).
    await ep.delegate(orch, "developer", _mk_developer_envelope("1.1"), task=task1)
    await ep.delegate(orch, "developer", _mk_developer_envelope("1.1"), task=task1)

    # First call on task 1.2 should be at base (no escalation).
    await ep.delegate(orch, "developer", _mk_developer_envelope("1.2"), task=task2)

    base_max_turns = adapter.calls[0].max_turns
    second_call_on_task1_max_turns = adapter.calls[1].max_turns
    first_call_on_task2_max_turns = adapter.calls[2].max_turns

    # Task 1.1's second call should be escalated.
    assert second_call_on_task1_max_turns > base_max_turns
    # Task 1.2's first call should NOT be escalated.
    assert first_call_on_task2_max_turns == base_max_turns


@pytest.mark.asyncio
async def test_escalation_resets_per_role(tmp_path: Path) -> None:
    """Escalation on developer for task 1.1 does NOT bleed into reviewer
    for task 1.1."""
    await _seed_plan(tmp_path, _mk_single_task_plan())
    adapter = StubAdapter(
        {
            "developer": [_max_turns_failure(), _max_turns_failure()],
            "reviewer": [_max_turns_failure()],
        }
    )
    orch = _make_orch(tmp_path, adapter)
    task = _mk_developer_task()

    # Two developer calls → tracker count = 2 for (1.1, developer).
    await ep.delegate(orch, "developer", _mk_developer_envelope(), task=task)
    await ep.delegate(orch, "developer", _mk_developer_envelope(), task=task)

    # First reviewer call on the same task should be at the reviewer's
    # base budget (no escalation cross-role).
    review_env = DelegationEnvelope(
        task_id="1.1", target_agent="reviewer", action="review"
    )
    await ep.delegate(orch, "reviewer", review_env)

    # Reviewer's base max_turns is 5 (per src/config/defaults.py;
    # bumped 3 → 5 in v0.31.0 Phase 1.4 to give reviewers more
    # headroom on non-trivial diffs).
    reviewer_call = next(c for c in adapter.calls if c.role == "reviewer")
    # Reviewer doesn't pass ``task=`` so spec_max_turns kicks in (=5).
    assert reviewer_call.max_turns == 5


@pytest.mark.asyncio
async def test_escalation_respects_ceiling(tmp_path: Path) -> None:
    """A tiny ceiling bound caps the escalated ``max_turns`` even when the
    raw curve would exceed it."""
    # Direct unit-test on the helper since the ceiling is exposed there;
    # the integration site reads cfg.budget_escalation.max_turns_ceiling
    # which is not in the default schema (operator-override surface).
    new_max, new_timeout = escalate_budget(
        50, 600, 2, max_turns_ceiling=20, timeout_s_ceiling=700
    )
    assert new_max == 20  # capped, would have been 100
    assert new_timeout == 700  # capped, would have been 900


def test_default_ceilings_are_sane() -> None:
    """Module-level defaults are tuned for production: 100 turns / 1h."""
    assert DEFAULT_MAX_TURNS_CEILING == 100
    assert DEFAULT_TIMEOUT_S_CEILING == 3600
