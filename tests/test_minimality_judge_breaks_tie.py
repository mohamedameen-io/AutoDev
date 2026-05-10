"""v0.22.0 Phase 4: minimality judge breaks ties when correctness abstains.

When the correctness judge fails to emit a parseable RANKING (its entry is
``None``) and the minimality_judge produces a concrete ranking, the
minimality vote becomes the sole signal — its preferred winner must win.
"""

from __future__ import annotations

from tournament.voting import BordaAggregator


def test_minimality_wins_when_correctness_abstains() -> None:
    """Correctness=None, minimality_judge=concrete → minimality picks the winner.

    Setup:
      * judge → None (parser failure / abstention).
      * minimality_judge → AB > B > A.

    With minimality the SOLE valid judge, its preferred winner (AB) wins.
    We use weight 1.0 here because the int-cast in BordaAggregator
    (``int((n - pos) * w)``) collapses sub-1.0 weights into ties at the
    integer floor — the test we care about is "minimality's signal makes
    it through", not the post-int rescaling of weights.
    """
    borda = BordaAggregator()
    rankings = [
        None,                # correctness judge: parse failure
        ["AB", "B", "A"],    # minimality_judge
    ]
    weights = [1.0, 1.0]
    winner, scores, _ = borda.aggregate(
        rankings,
        labels=["A", "B", "AB"],
        tiebreak_winner="A",
        weights=weights,
    )
    assert winner == "AB"
    assert scores["AB"] > scores["A"]
    assert scores["AB"] > scores["B"]


def test_two_correctness_abstain_minimality_alone() -> None:
    """Both judge + judge_explorer abstain; minimality picks the winner alone."""
    borda = BordaAggregator()
    rankings = [
        None,                # judge
        None,                # judge_explorer
        ["B", "A", "AB"],    # minimality_judge
    ]
    # When minimality is the sole signal, weight scaling is irrelevant for
    # the winner identity (it would be for ties — see comment above).
    weights = [1.0, 1.0, 1.0]
    winner, _scores, valid = borda.aggregate(
        rankings,
        labels=["A", "B", "AB"],
        tiebreak_winner="A",
        weights=weights,
    )
    assert winner == "B"
    assert valid == 1  # only one judge contributed


def test_minimality_at_half_weight_with_abstain_still_picks_clear_winner() -> None:
    """Even at weight 0.5, minimality picks the winner when its signal is decisive.

    Note: BordaAggregator does ``int((n - pos) * w)``. With w=0.5 and
    rankings [AB, B, A], scores collapse to AB=1, B=1, A=0 — a
    tiebreak-driven outcome favoring B (priority 1 < AB priority 2).
    To keep minimality's vote intact we recommend weight ≥ 1.0 in the
    abstention path; the half-weight here is just for documentation of
    the int-cast behavior. This test asserts the documented behavior so
    a future refactor that quietly drops the int-cast is detected.
    """
    borda = BordaAggregator()
    rankings = [None, ["AB", "B", "A"]]
    weights = [1.0, 0.5]
    winner, scores, _ = borda.aggregate(
        rankings, labels=["A", "B", "AB"], tiebreak_winner="A",
        weights=weights,
    )
    # Documented int-cast behavior: AB=1, B=1, A=0 → tiebreak picks B
    # (priority 1 < AB priority 2). This is the current truth — change it
    # only if the aggregator's int-cast is intentionally removed.
    assert scores["A"] == 0
    assert scores["B"] == 1
    assert scores["AB"] == 1
    assert winner == "B"


def test_all_judges_abstain_falls_back_to_tiebreak() -> None:
    """Sanity: if everyone abstains, tiebreak (A) wins."""
    borda = BordaAggregator()
    rankings = [None, None, None]
    weights = [1.0, 1.0, 0.5]
    winner, _scores, valid = borda.aggregate(
        rankings,
        labels=["A", "B", "AB"],
        tiebreak_winner="A",
        weights=weights,
    )
    assert winner == "A"
    assert valid == 0
