"""Tests for Borda aggregation & conservative tiebreak, ported from autoreason."""

from __future__ import annotations

import pytest
hypothesis = pytest.importorskip("hypothesis")
given = hypothesis.given
settings = hypothesis.settings
st = hypothesis.strategies

from tournament import aggregate_rankings


# ── Basic cases ───────────────────────────────────────────────────────────

def test_single_judge_abc_order() -> None:
    winner, scores, valid = aggregate_rankings(
        [["A", "B", "AB"]], labels=["A", "B", "AB"], tiebreak_winner="A"
    )
    assert winner == "A"
    assert scores == {"A": 3, "B": 2, "AB": 1}
    assert valid == 1


def test_three_judges_ab_wins() -> None:
    # Judge 1: AB > B > A (AB=3, B=2, A=1)
    # Judge 2: B  > AB > A (AB=2, B=3, A=1)
    # Judge 3: A  > AB > B (AB=2, B=1, A=3)
    # Totals: A=5, B=6, AB=7 → AB wins
    rankings = [
        ["AB", "B", "A"],
        ["B", "AB", "A"],
        ["A", "AB", "B"],
    ]
    winner, scores, valid = aggregate_rankings(
        rankings, labels=["A", "B", "AB"], tiebreak_winner="A"
    )
    assert winner == "AB"
    assert scores == {"A": 5, "B": 6, "AB": 7}
    assert valid == 3


def test_none_rankings_ignored_for_scores() -> None:
    rankings = [["A", "B", "AB"], None, ["A", "AB", "B"]]
    winner, scores, valid = aggregate_rankings(
        rankings, labels=["A", "B", "AB"], tiebreak_winner="A"
    )
    # Only 2 valid judges counted.
    assert valid == 2
    # A scores 3+3=6, B scores 2+1=3, AB scores 1+2=3.
    assert scores == {"A": 6, "B": 3, "AB": 3}
    assert winner == "A"


def test_all_none_rankings_fallback_to_tiebreak() -> None:
    winner, scores, valid = aggregate_rankings(
        [None, None, None], labels=["A", "B", "AB"], tiebreak_winner="A"
    )
    assert scores == {"A": 0, "B": 0, "AB": 0}
    assert valid == 0
    assert winner == "A"


def test_empty_rankings_fallback_to_tiebreak() -> None:
    winner, scores, valid = aggregate_rankings(
        [], labels=["A", "B", "AB"], tiebreak_winner="A"
    )
    assert scores == {"A": 0, "B": 0, "AB": 0}
    assert valid == 0
    assert winner == "A"


# ── Tiebreak behaviour ────────────────────────────────────────────────────

def test_tiebreak_A_beats_B_on_equal_scores() -> None:
    # Two judges, one prefers A > AB > B, other prefers B > AB > A.
    # A=3+1=4, B=1+3=4, AB=2+2=4 — three-way tie. With tiebreak A, A wins.
    rankings = [["A", "AB", "B"], ["B", "AB", "A"]]
    winner, scores, _ = aggregate_rankings(
        rankings, labels=["A", "B", "AB"], tiebreak_winner="A"
    )
    assert scores == {"A": 4, "B": 4, "AB": 4}
    assert winner == "A"


def test_tiebreak_AB_wins_over_A() -> None:
    # Same tie configuration, but tiebreak_winner="AB".
    rankings = [["A", "AB", "B"], ["B", "AB", "A"]]
    winner, scores, _ = aggregate_rankings(
        rankings, labels=["A", "B", "AB"], tiebreak_winner="AB"
    )
    assert scores == {"A": 4, "B": 4, "AB": 4}
    assert winner == "AB"


def test_no_tiebreak_uses_label_order() -> None:
    rankings = [["A", "AB", "B"], ["B", "AB", "A"]]
    winner, scores, _ = aggregate_rankings(
        rankings, labels=["A", "B", "AB"], tiebreak_winner=None
    )
    assert scores == {"A": 4, "B": 4, "AB": 4}
    # With no tiebreak winner, the first label in `labels` wins ties.
    assert winner == "A"


def test_unequal_scores_beat_tiebreak() -> None:
    # B wins decisively; tiebreak to A should NOT override.
    rankings = [["B", "AB", "A"], ["B", "AB", "A"]]
    winner, _, _ = aggregate_rankings(
        rankings, labels=["A", "B", "AB"], tiebreak_winner="A"
    )
    assert winner == "B"


# ── Five-way labels (autoreason 5-way judge) ───────────────────────────────

def test_five_way_aggregation() -> None:
    # Each judge ranks 5 proposals labelled A..E.
    rankings = [
        ["A", "B", "C", "D", "E"],   # A=5, B=4, C=3, D=2, E=1
        ["B", "A", "C", "D", "E"],   # A=4, B=5, C=3, D=2, E=1
    ]
    winner, scores, valid = aggregate_rankings(
        rankings, labels=["A", "B", "C", "D", "E"], tiebreak_winner="A"
    )
    assert scores == {"A": 9, "B": 9, "C": 6, "D": 4, "E": 2}
    assert valid == 2
    assert winner == "A"  # A and B tie; A wins by tiebreak


# ── Default labels ─────────────────────────────────────────────────────────

def test_default_labels() -> None:
    # labels=None should default to ["A", "B", "AB"].
    winner, scores, _ = aggregate_rankings([["A", "B", "AB"]])
    assert winner == "A"
    assert set(scores.keys()) == {"A", "B", "AB"}


# ── Property tests (via hypothesis) ────────────────────────────────────────

_LABELS = ["A", "B", "AB"]


@st.composite
def _rankings(draw: st.DrawFn, n_judges: int) -> list[list[str] | None]:
    rs = []
    for _ in range(n_judges):
        if draw(st.booleans()):
            rs.append(None)
        else:
            rs.append(draw(st.permutations(_LABELS)))
    return rs


@given(st.integers(min_value=0, max_value=10).flatmap(_rankings))
@settings(max_examples=200, deadline=None)
def test_score_sum_invariant(rankings: list[list[str] | None]) -> None:
    """Sum of Borda points across all labels equals `valid * n * (n+1) / 2`.

    With n=3 labels, each valid judge contributes 3 + 2 + 1 = 6 points.
    """
    _, scores, valid = aggregate_rankings(
        rankings, labels=_LABELS, tiebreak_winner="A"
    )
    n = len(_LABELS)
    expected_total = valid * n * (n + 1) // 2
    assert sum(scores.values()) == expected_total


@given(st.integers(min_value=1, max_value=10).flatmap(_rankings))
@settings(max_examples=200, deadline=None)
def test_winner_has_max_score_or_wins_tiebreak(
    rankings: list[list[str] | None],
) -> None:
    """Winner's score is the maximum; among ties, A wins if it tied."""
    winner, scores, valid = aggregate_rankings(
        rankings, labels=_LABELS, tiebreak_winner="A"
    )
    max_score = max(scores.values())
    assert scores[winner] == max_score
    if valid == 0:
        assert winner == "A"
    tied = [label for label, s in scores.items() if s == max_score]
    # If A is among the tied labels, the tiebreak must pick A.
    if "A" in tied and len(tied) > 1:
        assert winner == "A"


@given(st.integers(min_value=0, max_value=10).flatmap(_rankings))
@settings(max_examples=200, deadline=None)
def test_valid_count_matches_non_none(
    rankings: list[list[str] | None],
) -> None:
    _, _, valid = aggregate_rankings(
        rankings, labels=_LABELS, tiebreak_winner="A"
    )
    assert valid == sum(1 for r in rankings if r is not None)


# ── Autoreason golden-fixture regression ────────────────────────────────
# Reproduces Borda scores from a real autoreason run (pass 1 of paper's run).
# This guarantees our port matches the reference implementation bit-exactly.

import json as _json
from pathlib import Path as _Path


def test_autoreason_golden_pass_01_scores() -> None:
    """Regenerate scores from the recorded judge_details of a real pass."""
    fixture = (
        _Path(__file__).parent
        / "fixtures"
        / "autoreason"
        / "sample_run"
        / "pass_01"
        / "result.json"
    )
    if not fixture.exists():
        pytest.skip("autoreason golden fixture unavailable")

    data = _json.loads(fixture.read_text())
    expected_winner = data["winner"]
    expected_scores = data["scores"]

    rankings = [d["ranking"] for d in data["judge_details"]]
    winner, scores, _ = aggregate_rankings(
        rankings, labels=["A", "B", "AB"], tiebreak_winner="A"
    )
    assert scores == expected_scores
    assert winner == expected_winner


def test_autoreason_golden_history_every_pass() -> None:
    """Every pass's reported winner equals what we compute from its ranking.

    The paper fixture only stores `scores` and `winner` at the top level; we
    can't re-derive scores from history.json alone (the judge_details sit in
    each pass_NN/result.json), but we can at least verify that the winner
    reported is consistent with the scores reported (i.e. has max score, or
    ties with the incumbent tiebreak).
    """
    fixture = (
        _Path(__file__).parent
        / "fixtures"
        / "autoreason"
        / "sample_run"
        / "history.json"
    )
    if not fixture.exists():
        pytest.skip("autoreason golden fixture unavailable")

    history = _json.loads(fixture.read_text())
    for entry in history:
        scores = entry["scores"]
        winner = entry["winner"]
        max_score = max(scores.values())
        tied_at_max = [k for k, v in scores.items() if v == max_score]
        # Either unique winner OR winner is A (the tiebreak).
        if len(tied_at_max) == 1:
            assert tied_at_max[0] == winner
        else:
            assert "A" in tied_at_max
            assert winner == "A"
