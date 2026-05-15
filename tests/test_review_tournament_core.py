"""Core unit tests for :mod:`orchestrator.review_tournament_runner`.

Covers the pure-logic surfaces:

  * Borda + tiebreak invariants for the A/B/AB tournament.
  * ``_no_progress`` short-circuit when B and AB duplicate A.
  * ``_resolve_judge_cohort`` precedence (override vs default).

Integration-shaped behavior (full ``run_review_tournament`` flow with a
stub adapter, ledger emissions, evidence writes) lives in
``test_review_tournament_integration.py`` so this module stays fast and
free of orchestrator setup.
"""

from __future__ import annotations

import pytest

from orchestrator.review_tournament_runner import (
    _DEFAULT_REVIEW_JUDGE_COHORT,
    _no_progress,
    _resolve_judge_cohort,
)
from state.schemas import ReviewCandidate
from tournament.voting import BordaAggregator


_LABELS = ["A", "B", "AB"]


# ── Borda + tiebreak invariants ────────────────────────────────────────


def test_borda_tie_resolution_3way_picks_a() -> None:
    """A 3-way tie always resolves to A via ``tiebreak_winner='A'``.

    Each judge ranks a different candidate first; Borda points are
    perfectly symmetric (4-4-4 with three judges, three candidates).
    The tournament's conservative-tiebreak invariant promotes A.
    """
    rankings = [
        ["A", "B", "AB"],
        ["B", "AB", "A"],
        ["AB", "A", "B"],
    ]
    winner, scores, valid = BordaAggregator().aggregate(
        rankings, labels=_LABELS, tiebreak_winner="A"
    )
    assert winner == "A"
    # Every label gets the same total — invariant of cyclic ranking.
    assert scores["A"] == scores["B"] == scores["AB"]
    assert valid == 3


def test_convergence_on_a_win_round_one() -> None:
    """A wins round 1 unanimously → tournament should record streak == 1.

    The runner-side convergence check happens in
    :func:`run_review_tournament`; this test pins the aggregator
    invariant the runner depends on (A wins cleanly when judges agree).
    """
    rankings = [
        ["A", "B", "AB"],
        ["A", "AB", "B"],
        ["A", "B", "AB"],
    ]
    winner, _scores, valid = BordaAggregator().aggregate(
        rankings, labels=_LABELS, tiebreak_winner="A"
    )
    assert winner == "A"
    assert valid == 3


def test_two_rounds_then_converge_aggregator() -> None:
    """Round 1 B wins, round 2 A wins (unanimous).

    Smoke-tests the per-round Borda contract the runner runs twice. The
    runner-side ``a_streak`` accounting (k=2) is exercised by the
    integration test; this test pins the per-round arithmetic.
    """
    # Round 1: B is everybody's first → unanimous B win.
    r1_rankings = [
        ["B", "A", "AB"],
        ["B", "AB", "A"],
        ["B", "A", "AB"],
    ]
    w1, _s1, _v1 = BordaAggregator().aggregate(
        r1_rankings, labels=_LABELS, tiebreak_winner="A"
    )
    assert w1 == "B"
    # Round 2: A wins.
    r2_rankings = [
        ["A", "B", "AB"],
        ["A", "AB", "B"],
        ["A", "B", "AB"],
    ]
    w2, _s2, _v2 = BordaAggregator().aggregate(
        r2_rankings, labels=_LABELS, tiebreak_winner="A"
    )
    assert w2 == "A"


def test_all_judges_malformed_falls_to_tiebreak() -> None:
    """When every judge returns ``None`` (parse failure) the aggregator
    returns the tiebreak winner with all-zero scores.
    """
    rankings: list[list[str] | None] = [None, None, None]
    winner, scores, valid = BordaAggregator().aggregate(
        rankings, labels=_LABELS, tiebreak_winner="A"
    )
    assert winner == "A"
    assert scores == {"A": 0, "B": 0, "AB": 0}
    assert valid == 0


@pytest.mark.parametrize(
    "rankings",
    [
        [["A", "B", "AB"], ["A", "B", "AB"], ["A", "B", "AB"]],
        [["B", "A", "AB"], ["AB", "A", "B"], ["A", "AB", "B"]],
        [["AB", "B", "A"], None, ["A", "B", "AB"]],
        [None, None, ["A", "B", "AB"]],
    ],
)
def test_borda_score_invariant(rankings: list[list[str] | None]) -> None:
    """Sum of Borda scores equals ``valid_judges * len(labels)``.

    Property held: each valid judge contributes ``n + (n-1) + ... + 1``
    points distributed across the labels; with ``n=3`` that's 6 points
    per judge spread over A+B+AB. ``None`` rankings contribute 0.
    """
    _winner, scores, valid = BordaAggregator().aggregate(
        rankings, labels=_LABELS, tiebreak_winner="A"
    )
    expected_total = valid * (len(_LABELS) * (len(_LABELS) + 1) // 2)
    assert sum(scores.values()) == expected_total


# ── _no_progress short-circuit ─────────────────────────────────────────


def _cand(verdict: str, issues: list[str]) -> ReviewCandidate:
    return ReviewCandidate(
        diff_excerpt="diff",
        verdict=verdict,
        issues=issues,
        raw_response="raw",
    )


def test_all_candidates_identical_no_progress() -> None:
    """When B and AB exactly duplicate A's verdict + issues, the
    no-progress detector returns True so the runner can short-circuit
    the judge cohort and award A the round by tiebreak.
    """
    a = _cand("APPROVED", ["nit at file.py:10"])
    b = _cand("APPROVED", ["nit at file.py:10"])
    ab = _cand("APPROVED", ["nit at file.py:10"])
    assert _no_progress({"A": a, "B": b, "AB": ab}) is True


def test_no_progress_false_when_b_differs() -> None:
    """B raises an issue A didn't → no_progress is False."""
    a = _cand("APPROVED", [])
    b = _cand("NEEDS_CHANGES", ["missed race condition at q.py:42"])
    ab = _cand("APPROVED", [])
    assert _no_progress({"A": a, "B": b, "AB": ab}) is False


def test_no_progress_false_when_ab_differs() -> None:
    """AB synthesises a different verdict than A → no_progress is False."""
    a = _cand("APPROVED", [])
    b = _cand("APPROVED", [])
    ab = _cand("NEEDS_CHANGES", ["B's missed race condition"])
    assert _no_progress({"A": a, "B": b, "AB": ab}) is False


def test_no_progress_issue_set_equality() -> None:
    """Issue ORDER is irrelevant; SET equality drives the detector.

    A and B raise the same two issues in different order. The detector
    treats them as duplicates because the issue set is identical.
    """
    a = _cand("NEEDS_CHANGES", ["issue 1", "issue 2"])
    b = _cand("NEEDS_CHANGES", ["issue 2", "issue 1"])
    ab = _cand("NEEDS_CHANGES", ["issue 1", "issue 2"])
    assert _no_progress({"A": a, "B": b, "AB": ab}) is True


# ── _resolve_judge_cohort precedence ───────────────────────────────────


class _FakeCfg:
    def __init__(self, override: list[str] | None) -> None:
        self.review_judge_roles = override


def test_resolve_judge_cohort_uses_default() -> None:
    """No override → built-in 3-role cohort."""
    cohort = _resolve_judge_cohort(_FakeCfg(None))
    assert cohort == list(_DEFAULT_REVIEW_JUDGE_COHORT)
    assert "minimality_judge" in cohort
    assert "judge_explorer" in cohort


def test_resolve_judge_cohort_honours_override() -> None:
    """Operator override wins over the default."""
    custom = ["judge", "judge", "reviewer", "reviewer"]
    cohort = _resolve_judge_cohort(_FakeCfg(custom))
    assert cohort == custom
