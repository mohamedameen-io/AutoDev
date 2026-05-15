"""Tests for v0.32.0 Phase 1.2 — plan-phase architect budget escalation.

The plan-phase architect retry loop now feeds the same
:class:`BudgetEscalationTracker` instance as the per-task execute-phase
loop, but keys on the literal scope ``"plan_phase"`` so the two
ladders never collide.

Coverage:

* Repeated ``error_max_turns`` on the architect bumps ``max_turns`` on
  the next attempt and emits a ``plan_phase_budget_escalation``
  ledger op.
* Other failure subtypes (e.g. ``rate_limited``) reset the counter
  exactly the way the per-task escalator does — the policy is
  intentionally conservative.
* The plan-phase ladder is independent from any per-task scope.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from adapters.types import AgentResult
from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator.budget_escalation import BudgetEscalationTracker

from stub_adapter import StubAdapter, ok


_GOOD_PLAN_MD = """
# Plan: Add subtract

## Phase 1: Implement

### Task 1.1: real path
  - Description: refs a real file
  - Files: math.py
  - Acceptance:
    - [ ] passes
"""


def _bootstrap_git_repo_with_math_py(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=str(tmp_path), check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=str(tmp_path), check=True
    )
    (tmp_path / "math.py").write_text("def add(a, b): return a + b\n")
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
    subprocess.run(
        ["git", "commit", "-qm", "init"], cwd=str(tmp_path), check=True
    )


def _make_orch(cwd: Path, adapter: StubAdapter) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    registry = build_registry(cfg)
    return Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-plan-budget",
    )


def _max_turns_failure(text: str = "") -> AgentResult:
    return AgentResult(
        success=False,
        text=text,
        duration_s=0.01,
        error="hit max turns",
        subtype="error_max_turns",
    )


def _read_ledger_ops(tmp_path: Path) -> list[dict]:
    ledger = tmp_path / ".autodev" / "plan-ledger.jsonl"
    if not ledger.exists():
        return []
    return [json.loads(ln) for ln in ledger.read_text().splitlines() if ln.strip()]


# ---------------------------------------------------------------------------
# Tracker shape
# ---------------------------------------------------------------------------


def test_tracker_handles_plan_phase_scope_independently_from_task_scope() -> None:
    """The same role under different scope_ids gets independent counters."""
    tr = BudgetEscalationTracker()
    tr.record_failure("plan_phase", "architect", "error_max_turns")
    tr.record_failure("plan_phase", "architect", "error_max_turns")
    assert tr.current_attempt("plan_phase", "architect") == 2
    # Per-task scope is untouched.
    assert tr.current_attempt("1.1", "architect") == 0


def test_record_failure_is_an_alias_for_record_result() -> None:
    """Plan-phase scope uses the same semantics as per-task."""
    tr = BudgetEscalationTracker()
    tr.record_failure("plan_phase", "architect", "error_max_turns")
    assert tr.current_attempt("plan_phase", "architect") == 1
    # Non-max-turns subtype resets, mirroring record_result.
    tr.record_failure("plan_phase", "architect", "auth_failed")
    assert tr.current_attempt("plan_phase", "architect") == 0


def test_escalate_for_returns_escalated_budget() -> None:
    """``escalate_for`` reads the current attempt and applies the curve."""
    tr = BudgetEscalationTracker()
    tr.record_failure("plan_phase", "architect", "error_max_turns")
    new_max, _ = tr.escalate_for(
        "plan_phase", "architect", base_max_turns=10
    )
    # 10 * 1.5 = 15 (Phase 3 curve).
    assert new_max == 15


# ---------------------------------------------------------------------------
# End-to-end: plan-phase architect loop drives the tracker + ledger
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_phase_budget_escalates_on_repeated_failure(
    tmp_path: Path,
) -> None:
    """Two consecutive ``error_max_turns`` on the architect during the
    plan phase: the third architect dispatch must use a bumped
    ``max_turns`` and a ``plan_phase_budget_escalation`` op must land
    in the ledger."""
    _bootstrap_git_repo_with_math_py(tmp_path)
    adapter = StubAdapter(
        {
            "explorer": ok("found stuff"),
            "domain_expert": ok("ok"),
            "architect": [
                _max_turns_failure(),
                _max_turns_failure(),
                ok(_GOOD_PLAN_MD),
            ],
        }
    )
    orch = _make_orch(tmp_path, adapter)
    plan = await orch.plan("Add subtract")
    assert plan is not None
    architect_calls = [c for c in adapter.calls if c.role == "architect"]
    assert len(architect_calls) == 3
    # First call is at the architect's configured base. The second call
    # is at the existing retry_max bump (architect_spec.max_turns + 2)
    # — that's the v0.26.2 retry budget BEFORE Phase 1.2's escalator
    # kicks in. The escalator bumps the THIRD call further.
    second_max = architect_calls[1].max_turns
    third_max = architect_calls[2].max_turns
    # Escalation curve for attempt index 2 is 2.0× the base.
    # We assert ">=" rather than equality because the escalator multiplies
    # the escalator-resolved base, and the call site already passes
    # retry_max — so the actual third bump may exceed strict 2.0× of the
    # spec base. The contract is "bigger than the prior attempt".
    assert third_max > second_max, (
        f"Phase 1.2 should bump max_turns on the third attempt; "
        f"got second={second_max}, third={third_max}"
    )

    # Ledger emits one plan_phase_budget_escalation op for the third
    # attempt. Two failures happened, so the second attempt sees
    # _esc_attempt=1 (bumps once) and the third sees _esc_attempt=2
    # (bumps once more). At least one op landed.
    ops = _read_ledger_ops(tmp_path)
    escalation_ops = [
        o for o in ops if o.get("op") == "plan_phase_budget_escalation"
    ]
    assert len(escalation_ops) >= 1, (
        f"Phase 1.2 should emit at least one plan_phase_budget_escalation "
        f"op; got ops={[o.get('op') for o in ops]}"
    )
    payload = escalation_ops[-1]["payload"]
    assert payload["from_max_turns"] < payload["to_max_turns"]
    assert payload["reason"] == "architect_max_turns_recurrence"


@pytest.mark.asyncio
async def test_plan_phase_budget_does_not_escalate_on_non_max_turns_failures(
    tmp_path: Path,
) -> None:
    """Architect returning ``rate_limited`` shouldn't bump the budget.

    The escalator's policy is to escalate only on consecutive
    ``error_max_turns`` (the parse_failed bucket aside, which is
    folded into the same counter). Other infrastructure failures
    reset the counter so transient noise doesn't grant the architect
    unbounded budget.
    """
    _bootstrap_git_repo_with_math_py(tmp_path)
    adapter = StubAdapter(
        {
            "explorer": ok("found stuff"),
            "domain_expert": ok("ok"),
            "architect": [
                AgentResult(
                    success=False,
                    text="",
                    duration_s=0.01,
                    error="rate-limited",
                    subtype="rate_limited",
                ),
                AgentResult(
                    success=False,
                    text="",
                    duration_s=0.01,
                    error="rate-limited",
                    subtype="rate_limited",
                ),
                ok(_GOOD_PLAN_MD),
            ],
        }
    )
    orch = _make_orch(tmp_path, adapter)
    # The architect returning failed results without parseable plan
    # markdown will exhaust the retry loop or succeed on the third try
    # depending on the parsing path. Either way, NO
    # plan_phase_budget_escalation op should fire.
    try:
        await orch.plan("Add subtract")
    except Exception:
        # The retry loop may surface a parse error if the failed-result
        # text doesn't parse as a plan; we don't care for this test.
        pass

    ops = _read_ledger_ops(tmp_path)
    escalation_ops = [
        o for o in ops if o.get("op") == "plan_phase_budget_escalation"
    ]
    assert escalation_ops == [], (
        f"rate_limited subtype should NOT trigger plan-phase budget "
        f"escalation; got {escalation_ops}"
    )


@pytest.mark.asyncio
async def test_plan_phase_scope_does_not_pollute_per_task_tracker(
    tmp_path: Path,
) -> None:
    """A plan-phase architect failure does NOT bump the per-task scope
    for any task — they are independent ladders."""
    _bootstrap_git_repo_with_math_py(tmp_path)
    adapter = StubAdapter(
        {
            "explorer": ok("found stuff"),
            "domain_expert": ok("ok"),
            "architect": [_max_turns_failure(), ok(_GOOD_PLAN_MD)],
        }
    )
    orch = _make_orch(tmp_path, adapter)
    await orch.plan("Add subtract")
    tracker = orch._budget_escalation_tracker
    # plan_phase scope reflects the failure (then reset on success).
    # The exact final state depends on whether the second call cleared.
    # What matters is the per-task scope is at zero.
    assert tracker.current_attempt("1.1", "architect") == 0
    assert tracker.current_attempt("1.1", "developer") == 0
