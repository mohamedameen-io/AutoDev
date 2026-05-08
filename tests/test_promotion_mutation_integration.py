"""Tests for v0.19.0 promotion-grade mutation/holdout integrations."""

from __future__ import annotations

from dataclasses import dataclass

from plugins.registry import GateResult
from tournament.promotion import decide, is_suspiciously_perfect


@dataclass
class _Holdout:
    passed: bool
    test_count: int
    failure_count: int
    failure_summary: str


def _passing_gates() -> list[GateResult]:
    return [GateResult(passed=True, details="ok")]


def test_dev_best_low_kill_rate_demands_repeat() -> None:
    decision = decide("dev_best", _passing_gates(), kill_rate=0.3)
    assert decision.action == "demand_repeat"
    assert "test sufficiency" in decision.reason


def test_dev_best_high_kill_rate_normal_ladder() -> None:
    decision = decide("dev_best", _passing_gates(), kill_rate=0.85)
    assert decision.action == "demand_repeat"
    assert "test sufficiency" not in decision.reason


def test_kill_rate_one_is_suspicious() -> None:
    """Perfect kill rate flagged by is_suspiciously_perfect."""
    assert is_suspiciously_perfect(_passing_gates(), kill_rate=1.0)


def test_kill_rate_below_one_not_inherently_suspicious() -> None:
    assert not is_suspiciously_perfect(
        [GateResult(passed=True, details="ok 5 mutants killed")],
        kill_rate=0.85,
    )


def test_dev_best_perfect_kill_rate_demands_repeat() -> None:
    """kill_rate==1.0 → suspicious → demand_repeat with that reason."""
    decision = decide("dev_best", _passing_gates(), kill_rate=1.0)
    assert decision.action == "demand_repeat"
    assert "suspicious" in decision.reason


def test_repeated_with_failed_holdout_no_change() -> None:
    holdout = _Holdout(
        passed=False, test_count=10, failure_count=2, failure_summary="..."
    )
    decision = decide("repeated", _passing_gates(), holdout_result=holdout)
    assert decision.action == "no_change"
    assert "holdout" in decision.reason


def test_repeated_with_passing_holdout_promotes() -> None:
    holdout = _Holdout(
        passed=True, test_count=10, failure_count=0, failure_summary=""
    )
    decision = decide("repeated", _passing_gates(), holdout_result=holdout)
    assert decision.action == "promote_to_eligible"


def test_repeated_without_holdout_promotes_legacy() -> None:
    """No holdout → ladder behaves as before (promote)."""
    decision = decide("repeated", _passing_gates())
    assert decision.action == "promote_to_eligible"


def test_dev_best_failed_gate_no_change_takes_precedence() -> None:
    """Even with low kill rate, a failed gate halts the ladder."""
    failing = [GateResult(passed=False, details="lint error")]
    decision = decide("dev_best", failing, kill_rate=0.1)
    assert decision.action == "no_change"
    assert "gate" in decision.reason
