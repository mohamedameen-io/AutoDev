"""RECOVERY-CONTRACT §7 Step 8 — OVERSIZED_INPUT failure class + BOUND_INPUT.

The A4 root cause: when a role (esp. ``critic_t``) is fed an OVERSIZED prompt it
burns turns reading tools/context and hits ``error_max_turns``. Today
``budget_escalation`` then grants it MORE turns — the *wrong* direction. Step 8
adds an :data:`OVERSIZED_INPUT` failure class that:

* is PRODUCED (non-inert) when an ``error_max_turns`` result arrives with a
  prompt whose length exceeds ``cfg.budget_escalation.oversized_input_char_threshold``;
* does NOT widen turns on the next dispatch (the budget-direction fix); and
* routes to a BOUND_INPUT resolver ladder (``narrow_scope`` -> ``ask_human``)
  that reduces input rather than adding turns.

This module proves all four with engagement tests (a real producer fires, the
budget gate is direction-correct AND specific, the ladder bounds input) plus a
broken-control that reverts the size check and shows the budget escalates again.

Tests are intentionally driven through the real ``delegate()`` dispatch path
(via ``StubAdapter``) so the producer is exercised end-to-end, not stubbed.
"""

from __future__ import annotations

import datetime as _dt
import math
from pathlib import Path
from typing import Any

import pytest

from adapters.types import AgentResult
from agents import build_registry
from config.defaults import default_config
from orchestrator import execute_phase as ep
from orchestrator import failure_classes as fc
from orchestrator import Orchestrator
from orchestrator.budget_escalation import BudgetEscalationTracker
from orchestrator.delegation_envelope import DelegationEnvelope
from state.plan_manager import PlanManager
from state.schemas import (
    AcceptanceCriterion,
    BlockerContext,
    Phase,
    Plan,
    Task,
)
from stub_adapter import StubAdapter

from orchestrator.blocker_resolver import deterministic_action


# ---------------------------------------------------------------------------
# Failure class registration
# ---------------------------------------------------------------------------


def test_oversized_input_is_a_known_failure_class() -> None:
    """The new class exists, is in the taxonomy, and is NOT structural."""
    assert fc.OVERSIZED_INPUT == "oversized_input"
    assert fc.OVERSIZED_INPUT in fc.ALL_FAILURE_CLASSES
    assert fc.is_known(fc.OVERSIZED_INPUT) is True
    assert fc.classify("oversized_input") == fc.OVERSIZED_INPUT
    # Must NOT be structural: the recovery is task-local (bound the input),
    # not a phase-wide re-plan / intentional quarantine.
    assert fc.OVERSIZED_INPUT not in fc.STRUCTURAL_FAILURE_CLASSES


# ---------------------------------------------------------------------------
# Pure producer logic: classify_max_turns_failure
# ---------------------------------------------------------------------------


def test_classifier_oversized_when_prompt_exceeds_threshold() -> None:
    """A big prompt + error_max_turns -> OVERSIZED_INPUT (not guardrail)."""
    out = fc.classify_max_turns_failure(prompt_len=500_000, threshold=200_000)
    assert out == fc.OVERSIZED_INPUT


def test_classifier_guardrail_when_prompt_under_threshold() -> None:
    """A small prompt + error_max_turns -> the normal guardrail class.

    This is the non-vacuity proof: the producer fires only on real bloat.
    """
    out = fc.classify_max_turns_failure(prompt_len=1_000, threshold=200_000)
    assert out == fc.GUARDRAIL_EXCEEDED
    assert out != fc.OVERSIZED_INPUT


def test_classifier_boundary_is_inclusive() -> None:
    """At exactly the threshold, the input is treated as oversized."""
    assert (
        fc.classify_max_turns_failure(prompt_len=200_000, threshold=200_000)
        == fc.OVERSIZED_INPUT
    )
    assert (
        fc.classify_max_turns_failure(prompt_len=199_999, threshold=200_000)
        == fc.GUARDRAIL_EXCEEDED
    )


# ---------------------------------------------------------------------------
# Budget tracker: oversized state machine (direction fix)
# ---------------------------------------------------------------------------


def test_tracker_marks_oversized_on_oversized_max_turns() -> None:
    """An oversized error_max_turns result flips the per-pair oversized flag."""
    tr = BudgetEscalationTracker()
    tr.record_result("1.1", "critic_t", "error_max_turns", oversized=True)
    assert tr.is_oversized("1.1", "critic_t") is True
    # Still counts as a max-turns cycle for exhaustion bookkeeping.
    assert tr.current_attempt("1.1", "critic_t") == 1


def test_tracker_not_oversized_on_normal_max_turns() -> None:
    """A normal (non-oversized) error_max_turns does NOT set the flag."""
    tr = BudgetEscalationTracker()
    tr.record_result("1.1", "developer", "error_max_turns", oversized=False)
    assert tr.is_oversized("1.1", "developer") is False
    assert tr.current_attempt("1.1", "developer") == 1


def test_tracker_clears_oversized_on_reset() -> None:
    """Success / other-subtype clears both the counter and the oversized flag."""
    tr = BudgetEscalationTracker()
    tr.record_result("1.1", "critic_t", "error_max_turns", oversized=True)
    tr.record_result("1.1", "critic_t", "success")
    assert tr.is_oversized("1.1", "critic_t") is False
    assert tr.current_attempt("1.1", "critic_t") == 0


# ---------------------------------------------------------------------------
# Integration helpers (mirror tests/test_orchestrator_budget_escalation.py)
# ---------------------------------------------------------------------------


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_single_task_plan(task_id: str = "1.1") -> Plan:
    return Plan(
        plan_id="p-oversized",
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
    # Pin a known threshold so the test controls the producer boundary.
    assert cfg.budget_escalation is not None
    cfg.budget_escalation.oversized_input_char_threshold = 50_000
    registry = build_registry(cfg)
    return Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-oversized",
    )


async def _seed_plan(cwd: Path, plan: Plan) -> None:
    pm = PlanManager(cwd, session_id="sess-init")
    await pm.init_plan(plan)


def _max_turns_failure(text: str = "") -> Any:
    return AgentResult(
        success=False,
        text=text,
        duration_s=0.01,
        error="hit max turns",
        subtype="error_max_turns",
    )


# ---------------------------------------------------------------------------
# PRODUCER (non-inert): delegate() classifies an oversized max-turns failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_producer_marks_oversized_on_big_prompt(tmp_path: Path) -> None:
    """A real ``delegate()`` dispatch with a HUGE prompt + error_max_turns flips
    the tracker's oversized flag (the producer fires on real bloat)."""
    await _seed_plan(tmp_path, _mk_single_task_plan())
    adapter = StubAdapter({"developer": [_max_turns_failure()]})
    orch = _make_orch(tmp_path, adapter)
    env = _mk_developer_envelope()
    task = _mk_developer_task()
    # Inflate the dispatched prompt past the 50_000-char threshold via
    # extra_context (delegate appends it directly into inv.prompt).
    big = "X" * 60_000

    await ep.delegate(orch, "developer", env, extra_context=big, task=task)

    # The adapter actually saw an oversized prompt (size source is real).
    assert len(adapter.calls[0].prompt) >= 50_000
    tracker = orch._budget_escalation_tracker
    assert tracker.is_oversized("1.1", "developer") is True


@pytest.mark.asyncio
async def test_producer_does_not_mark_oversized_on_small_prompt(
    tmp_path: Path,
) -> None:
    """The same dispatch with a SMALL prompt + error_max_turns does NOT mark
    oversized (non-vacuity: the producer is keyed on real size, not the
    subtype alone)."""
    await _seed_plan(tmp_path, _mk_single_task_plan())
    adapter = StubAdapter({"developer": [_max_turns_failure()]})
    orch = _make_orch(tmp_path, adapter)
    env = _mk_developer_envelope()
    task = _mk_developer_task()

    await ep.delegate(orch, "developer", env, task=task)

    assert len(adapter.calls[0].prompt) < 50_000
    tracker = orch._budget_escalation_tracker
    assert tracker.is_oversized("1.1", "developer") is False


# ---------------------------------------------------------------------------
# BUDGET DIRECTION: oversized does NOT widen turns; normal max-turns DOES
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oversized_does_not_escalate_turns(tmp_path: Path) -> None:
    """Two consecutive oversized error_max_turns: the SECOND dispatch must NOT
    get more turns than the first (BOUND_INPUT, not more budget)."""
    await _seed_plan(tmp_path, _mk_single_task_plan())
    adapter = StubAdapter(
        {"developer": [_max_turns_failure(), _max_turns_failure()]}
    )
    orch = _make_orch(tmp_path, adapter)
    env = _mk_developer_envelope()
    task = _mk_developer_task()
    big = "X" * 60_000

    await ep.delegate(orch, "developer", env, extra_context=big, task=task)
    base_max_turns = adapter.calls[0].max_turns

    await ep.delegate(orch, "developer", env, extra_context=big, task=task)
    second_max_turns = adapter.calls[1].max_turns

    # The WRONG direction would be ceil(base * 1.5). The fix keeps it at base.
    assert second_max_turns == base_max_turns, (
        "oversized-input cause must NOT widen turns (it should bound input)"
    )


@pytest.mark.asyncio
async def test_normal_max_turns_still_escalates(tmp_path: Path) -> None:
    """Control: a NORMAL (small-prompt) consecutive error_max_turns still
    escalates turns 1.5× on the second dispatch — proving the gate is specific
    to oversized input, not a blanket disable of escalation."""
    await _seed_plan(tmp_path, _mk_single_task_plan())
    adapter = StubAdapter(
        {"developer": [_max_turns_failure(), _max_turns_failure()]}
    )
    orch = _make_orch(tmp_path, adapter)
    env = _mk_developer_envelope()
    task = _mk_developer_task()

    await ep.delegate(orch, "developer", env, task=task)
    base_max_turns = adapter.calls[0].max_turns

    await ep.delegate(orch, "developer", env, task=task)
    second_max_turns = adapter.calls[1].max_turns

    assert second_max_turns == math.ceil(base_max_turns * 1.5), (
        "a normal (non-oversized) error_max_turns must still escalate turns"
    )


# ---------------------------------------------------------------------------
# LADDER: oversized_input -> bounding action -> ask_human (NOT escalate_budget)
# ---------------------------------------------------------------------------


def test_ladder_first_rung_bounds_input_not_budget() -> None:
    """The first rung for oversized_input reduces input (narrow_scope) and must
    NOT widen the budget (escalate_budget is the wrong direction)."""
    ctx = BlockerContext(failure_class=fc.OVERSIZED_INPUT)
    action = deterministic_action(ctx)
    assert action is not None
    assert action.action != "escalate_budget"
    assert action.action == "narrow_scope"
    # The direction must signal input-bounding so the call site re-dispatches
    # with reduced scope rather than the same bloated prompt.
    assert action.params.get("direction") == "bound_input"


def test_ladder_terminates_in_ask_human() -> None:
    """After the bounding rung is tried, the ladder terminates in ask_human."""
    ctx = BlockerContext(
        failure_class=fc.OVERSIZED_INPUT,
        recovery_already_tried=["narrow_scope"],
    )
    action = deterministic_action(ctx)
    assert action is not None
    assert action.action == "ask_human"


def test_ladder_never_uses_escalate_budget() -> None:
    """No rung in the oversized_input ladder may be escalate_budget."""
    for tried in ([], ["narrow_scope"], ["narrow_scope", "ask_human"]):
        ctx = BlockerContext(
            failure_class=fc.OVERSIZED_INPUT, recovery_already_tried=tried
        )
        action = deterministic_action(ctx)
        assert action is not None
        assert action.action != "escalate_budget"


# ---------------------------------------------------------------------------
# BROKEN-CONTROL: revert the size check -> the budget-direction test goes red
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_broken_control_always_guardrail_escalates_oversized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revert the producer's size check (force every max-turns to classify as
    the guardrail class, never oversized). The oversized dispatch then escalates
    turns again — proving the size check is load-bearing, not decorative."""
    # Monkeypatch the classifier so it NEVER returns OVERSIZED_INPUT — i.e.
    # the pre-Step-8 behaviour where oversized input just looked like a
    # guardrail exhaustion.
    monkeypatch.setattr(
        fc,
        "classify_max_turns_failure",
        lambda prompt_len, threshold: fc.GUARDRAIL_EXCEEDED,
    )
    # The execute_phase module imports failure_classes as ``_fcls``; patch the
    # bound name there too so the producer call site sees the reverted check.
    monkeypatch.setattr(
        ep._fcls,
        "classify_max_turns_failure",
        lambda prompt_len, threshold: fc.GUARDRAIL_EXCEEDED,
    )

    await _seed_plan(tmp_path, _mk_single_task_plan())
    adapter = StubAdapter(
        {"developer": [_max_turns_failure(), _max_turns_failure()]}
    )
    orch = _make_orch(tmp_path, adapter)
    env = _mk_developer_envelope()
    task = _mk_developer_task()
    big = "X" * 60_000

    await ep.delegate(orch, "developer", env, extra_context=big, task=task)
    base_max_turns = adapter.calls[0].max_turns
    tracker = orch._budget_escalation_tracker
    # With the size check reverted, the oversized state is never set.
    assert tracker.is_oversized("1.1", "developer") is False

    await ep.delegate(orch, "developer", env, extra_context=big, task=task)
    second_max_turns = adapter.calls[1].max_turns

    # The bug returns: turns escalate on oversized input (the WRONG direction).
    assert second_max_turns == math.ceil(base_max_turns * 1.5)
