"""Tests for the v0.16.0 promotion-grade ladder helpers.

Covers :func:`tournament.promotion.decide` and the
:func:`is_suspiciously_perfect` heuristic. The ladder rules:

  * ``dev_best`` + all gates passed → ``demand_repeat``
  * ``pending_repeat`` + same candidate wins second time → ``promote_to_repeated``
  * ``repeated`` + holdout-equivalent run passes → ``promote_to_eligible``
  * Otherwise → ``no_change``

The suspicious-perfect override forces ``demand_repeat`` regardless of grade
when ALL gates passed AND any gate's ``details`` contains "0 errors" /
"100%" / "no findings".
"""

from __future__ import annotations

from plugins.registry import GateResult
from tournament.promotion import (
    PromotionDecision,
    decide,
    is_suspiciously_perfect,
)


# ── decide() ladder rules ────────────────────────────────────────────────


def test_decide_dev_best_with_passing_gates_demands_repeat() -> None:
    """First-time winner at dev_best with clean gates → demand_repeat."""
    gates = [GateResult(passed=True, details="lint clean")]
    out = decide(grade="dev_best", gate_results=gates)
    assert isinstance(out, PromotionDecision)
    assert out.action == "demand_repeat"
    assert "repeat" in out.reason.lower()


def test_decide_pending_repeat_same_candidate_promotes_to_repeated() -> None:
    """A pending_repeat that wins again is now confirmed → promote_to_repeated."""
    gates = [GateResult(passed=True, details="lint clean")]
    out = decide(grade="pending_repeat", gate_results=gates)
    assert out.action == "promote_to_repeated"


def test_decide_repeated_with_holdout_promotes_to_eligible() -> None:
    """A ``repeated`` candidate that passes its holdout-equivalent → eligible."""
    gates = [GateResult(passed=True, details="lint clean")]
    out = decide(grade="repeated", gate_results=gates)
    assert out.action == "promote_to_eligible"


def test_decide_returns_no_change_when_a_gate_failed_at_dev_best() -> None:
    """A failed gate must NOT be promoted regardless of grade."""
    gates = [
        GateResult(passed=True, details="lint clean"),
        GateResult(passed=False, details="tests failed"),
    ]
    out = decide(grade="dev_best", gate_results=gates)
    assert out.action == "no_change"


def test_decide_returns_no_change_for_unknown_grade() -> None:
    """Unknown grades hit the safe fallback."""
    gates = [GateResult(passed=True, details="ok")]
    out = decide(grade="totally-bogus", gate_results=gates)
    assert out.action == "no_change"


# ── is_suspiciously_perfect() heuristic ────────────────────────────────────


def test_is_suspiciously_perfect_detects_zero_errors() -> None:
    gates = [GateResult(passed=True, details="lint: 0 errors")]
    assert is_suspiciously_perfect(gates) is True


def test_is_suspiciously_perfect_detects_hundred_percent() -> None:
    gates = [
        GateResult(passed=True, details="ok"),
        GateResult(passed=True, details="coverage: 100% lines covered"),
    ]
    assert is_suspiciously_perfect(gates) is True


def test_is_suspiciously_perfect_detects_no_findings() -> None:
    gates = [GateResult(passed=True, details="secretscan: no findings")]
    assert is_suspiciously_perfect(gates) is True


def test_is_suspiciously_perfect_negative_when_warnings_present() -> None:
    """Realistic gate output (with warnings) is NOT suspicious."""
    gates = [
        GateResult(passed=True, details="lint: 3 warnings"),
        GateResult(passed=True, details="coverage: 87%"),
    ]
    assert is_suspiciously_perfect(gates) is False


def test_is_suspiciously_perfect_negative_when_any_gate_failed() -> None:
    """If any gate failed, the cohort is not suspicious — it's outright bad."""
    gates = [
        GateResult(passed=True, details="0 errors"),
        GateResult(passed=False, details="tests failed"),
    ]
    assert is_suspiciously_perfect(gates) is False


def test_is_suspiciously_perfect_empty_list_negative() -> None:
    """Empty gate list short-circuits to False (no signal to be suspicious)."""
    assert is_suspiciously_perfect([]) is False


# ── decide() override on suspicious-perfect ───────────────────────────────


def test_decide_overrides_to_demand_repeat_on_suspicious() -> None:
    """``dev_best`` + suspicious-perfect always demands a repeat (even when the
    rule would already suggest demand_repeat — the override carries a
    distinct reason for telemetry)."""
    gates = [GateResult(passed=True, details="0 errors")]
    out = decide(grade="dev_best", gate_results=gates)
    assert out.action == "demand_repeat"
    assert "suspicious" in out.reason.lower()


def test_decide_suspicious_promotes_pending_repeat_unchanged() -> None:
    """Suspicious-perfect override applies only at ``dev_best``. A
    ``pending_repeat`` candidate with suspicious gates still gets promoted
    to ``repeated`` (the second pass IS the demanded repeat)."""
    gates = [GateResult(passed=True, details="0 errors found")]
    out = decide(grade="pending_repeat", gate_results=gates)
    assert out.action == "promote_to_repeated"
