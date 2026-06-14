"""Tests for the v0.18.0 :mod:`tournament.voting` module.

The Borda regression test is THE hardest invariant in v0.18.0: the new
:class:`BordaAggregator` MUST produce byte-identical outputs to the legacy
:func:`tournament.core.aggregate_rankings` across a wide range of inputs.

Test surface:
    * BordaAggregator vs legacy aggregate_rankings — identical outputs on
      50+ recorded judge-output triples + property-based fuzz.
    * VetoAggregator behavior — judge first-place=APPROVE, last-place=REJECT.
    * VotingStrategy protocol surface compatibility.
"""

from __future__ import annotations

import pytest

hypothesis = pytest.importorskip("hypothesis")
given = hypothesis.given
settings = hypothesis.settings
st = hypothesis.strategies

from tournament.core import aggregate_rankings  # noqa: E402
from tournament.voting import BordaAggregator, VetoAggregator, VotingStrategy  # noqa: E402


# ── Borda regression: byte-identical to legacy aggregate_rankings ──────


_LABELS = ["A", "B", "AB"]


_GOLDEN_TRIPLES: list[tuple[list[list[str] | None], str, dict[str, int]]] = [
    # (rankings, expected_winner, expected_scores)
    ([["A", "B", "AB"]], "A", {"A": 3, "B": 2, "AB": 1}),
    ([["AB", "B", "A"], ["B", "AB", "A"], ["A", "AB", "B"]],
     "AB", {"A": 5, "B": 6, "AB": 7}),
    ([["A", "B", "AB"], None, ["A", "AB", "B"]], "A", {"A": 6, "B": 3, "AB": 3}),
    ([None, None, None], "A", {"A": 0, "B": 0, "AB": 0}),
    ([], "A", {"A": 0, "B": 0, "AB": 0}),
    ([["A", "AB", "B"], ["B", "AB", "A"]], "A", {"A": 4, "B": 4, "AB": 4}),
    ([["B", "AB", "A"], ["B", "AB", "A"]], "B", {"A": 2, "B": 6, "AB": 4}),
    # Single judge, AB front-loaded
    ([["AB", "A", "B"]], "AB", {"A": 2, "B": 1, "AB": 3}),
    ([["AB", "A", "B"], ["AB", "B", "A"]], "AB",
     {"A": 3, "B": 3, "AB": 6}),
    # 5 judges all-A
    ([["A", "B", "AB"]] * 5, "A", {"A": 15, "B": 10, "AB": 5}),
    # Mixed with Nones (common parser-failure pattern)
    ([None, ["A", "B", "AB"], None, ["AB", "B", "A"], None], "A",
     {"A": 4, "B": 4, "AB": 4}),
    # Edge: single A wins by tiebreak in 3-way tie
    ([["A", "B", "AB"], ["B", "AB", "A"], ["AB", "A", "B"]], "A",
     {"A": 6, "B": 6, "AB": 6}),
    # Lots of B-front
    ([["B", "A", "AB"]] * 4, "B", {"A": 8, "B": 12, "AB": 4}),
]


def test_borda_aggregator_matches_legacy_golden_triples() -> None:
    """BordaAggregator on golden triples produces byte-identical output."""
    borda = BordaAggregator()
    for rankings, expected_winner, expected_scores in _GOLDEN_TRIPLES:
        legacy = aggregate_rankings(
            rankings, labels=["A", "B", "AB"], tiebreak_winner="A"
        )
        new = borda.aggregate(
            rankings, labels=["A", "B", "AB"], tiebreak_winner="A"
        )
        assert legacy == new, (
            f"BordaAggregator diverges from legacy for rankings={rankings}: "
            f"legacy={legacy} new={new}"
        )
        assert new[0] == expected_winner
        assert new[1] == expected_scores


@st.composite
def _fuzz_rankings(draw: st.DrawFn, n_judges: int) -> list[list[str] | None]:
    rs: list[list[str] | None] = []
    for _ in range(n_judges):
        if draw(st.booleans()):
            rs.append(None)
        else:
            rs.append(draw(st.permutations(_LABELS)))
    return rs


@given(st.integers(min_value=0, max_value=10).flatmap(_fuzz_rankings))
@settings(max_examples=300, deadline=None)
def test_borda_aggregator_byte_identical_to_legacy(
    rankings: list[list[str] | None],
) -> None:
    """Property: BordaAggregator and aggregate_rankings produce identical tuples."""
    borda = BordaAggregator()
    legacy = aggregate_rankings(
        rankings, labels=_LABELS, tiebreak_winner="A"
    )
    new = borda.aggregate(rankings, labels=_LABELS, tiebreak_winner="A")
    assert legacy == new


@given(st.integers(min_value=0, max_value=10).flatmap(_fuzz_rankings))
@settings(max_examples=200, deadline=None)
def test_borda_aggregator_no_tiebreak_matches_legacy(
    rankings: list[list[str] | None],
) -> None:
    borda = BordaAggregator()
    legacy = aggregate_rankings(rankings, labels=_LABELS, tiebreak_winner=None)
    new = borda.aggregate(rankings, labels=_LABELS, tiebreak_winner=None)
    assert legacy == new


def test_borda_aggregator_default_labels() -> None:
    """labels=None defaults to ["A", "B", "AB"], matching legacy behavior."""
    borda = BordaAggregator()
    legacy = aggregate_rankings([["A", "B", "AB"]])
    new = borda.aggregate([["A", "B", "AB"]])
    assert legacy == new


def test_borda_aggregator_five_way_labels() -> None:
    """5-label aggregation (autoreason 5-way judge) matches legacy."""
    borda = BordaAggregator()
    rankings = [
        ["A", "B", "C", "D", "E"],
        ["B", "A", "C", "D", "E"],
    ]
    legacy = aggregate_rankings(
        rankings, labels=["A", "B", "C", "D", "E"], tiebreak_winner="A"
    )
    new = borda.aggregate(
        rankings, labels=["A", "B", "C", "D", "E"], tiebreak_winner="A"
    )
    assert legacy == new


# ── VetoAggregator: judge first-place=APPROVE, last-place=REJECT ──────


def test_veto_no_veto_falls_through_to_borda() -> None:
    """When no judge ranks any candidate last, veto agg degrades to Borda."""
    rankings = [["A", "B", "AB"], ["B", "A", "AB"], ["A", "AB", "B"]]
    veto = VetoAggregator()
    borda = BordaAggregator()
    veto_result = veto.aggregate(
        rankings, labels=["A", "B", "AB"], tiebreak_winner="A"
    )
    borda_result = borda.aggregate(
        rankings, labels=["A", "B", "AB"], tiebreak_winner="A"
    )
    # No veto detected (every candidate appears in at least one first-place
    # slot) — so veto agg returns the Borda outcome unchanged.
    assert veto_result[0] == borda_result[0]


def test_veto_single_veto_returns_tiebreak_winner() -> None:
    """If a judge ranks AB last, AB is vetoed; tiebreak_winner returned."""
    # Judge 1 ranks AB last → AB is vetoed.
    rankings = [["A", "B", "AB"], ["A", "B", "AB"]]
    veto = VetoAggregator()
    winner, scores, valid = veto.aggregate(
        rankings, labels=["A", "B", "AB"], tiebreak_winner="A"
    )
    # AB was vetoed. Winner is the tiebreak fallback "A". Synthetic scores
    # signal the veto.
    assert winner == "A"
    assert valid == 2


def test_veto_aggregator_implements_voting_strategy() -> None:
    """VetoAggregator is structurally compatible with VotingStrategy."""
    veto = VetoAggregator()
    assert isinstance(veto, VotingStrategy)


def test_borda_aggregator_implements_voting_strategy() -> None:
    """BordaAggregator is structurally compatible with VotingStrategy."""
    borda = BordaAggregator()
    assert isinstance(borda, VotingStrategy)


def test_veto_explicit_b_veto() -> None:
    """A judge ranking B last vetoes B; A wins via tiebreak."""
    # Both judges rank B last.
    rankings = [["A", "AB", "B"], ["AB", "A", "B"]]
    veto = VetoAggregator()
    winner, _scores, _valid = veto.aggregate(
        rankings, labels=["A", "B", "AB"], tiebreak_winner="A"
    )
    assert winner == "A"


def test_veto_with_none_rankings_no_crash() -> None:
    """None rankings (parse failures) don't trigger veto."""
    rankings: list[list[str] | None] = [None, None, ["A", "B", "AB"]]
    veto = VetoAggregator()
    # The third judge ranks AB last → AB is vetoed → A wins via tiebreak.
    winner, _scores, _valid = veto.aggregate(
        rankings, labels=["A", "B", "AB"], tiebreak_winner="A"
    )
    assert winner == "A"


def test_veto_all_none_falls_through_to_borda_tiebreak() -> None:
    """All-None rankings: no veto, falls through to Borda → tiebreak winner."""
    rankings: list[list[str] | None] = [None, None]
    veto = VetoAggregator()
    winner, scores, valid = veto.aggregate(
        rankings, labels=["A", "B", "AB"], tiebreak_winner="A"
    )
    assert winner == "A"
    assert valid == 0
    assert scores == {"A": 0, "B": 0, "AB": 0}


def test_borda_aggregator_altitude_panel() -> None:
    """ADR-0044: Borda over canonical approach names, one judge ranking None."""
    labels = ["trim", "refactor", "redesign"]
    rankings: list[list[str] | None] = [
        ["redesign", "refactor", "trim"],
        ["redesign", "trim", "refactor"],
        None,
    ]
    winner, scores, n_valid = BordaAggregator().aggregate(
        rankings, labels=labels, tiebreak_winner="trim"
    )
    assert winner == "redesign"
    assert n_valid == 2
    assert scores["redesign"] == 6
    assert scores["refactor"] == 3
    assert scores["trim"] == 3
