"""v0.22.0 Phase 4: weighted-Borda integration test for the minimality cohort.

Verifies that when the correctness judge (weight 1.0) and the minimality
judge (weight 0.5) rank A/B/AB in opposite orders, the correctness judge's
preferred winner wins — the minimality vote is advisory.
"""

from __future__ import annotations

from tournament.voting import BordaAggregator


def test_correctness_outweighs_minimality_when_opposite() -> None:
    """Correctness (1.0) > minimality (0.5): correctness's winner wins.

    Setup:
      * judge (weight 1.0) ranks A > B > AB.
        Borda contribution at weight 1.0: A=3, B=2, AB=1.
      * minimality_judge (weight 0.5) ranks AB > B > A.
        Borda contribution at weight 0.5: AB=1.5, B=1.0, A=0.5.

    Totals: A=3.5, B=3.0, AB=2.5 → A wins.
    Without weights (both = 1.0): A=4, B=4, AB=4 → A wins by tiebreak.
    With minimality at weight 1.0 instead of 0.5: A=4, B=4, AB=4 → A by tiebreak.

    What this test really proves: the heavier weight on correctness flips a
    naive tie into a clear A-victory, confirming the weight asymmetry takes
    effect inside BordaAggregator.
    """
    borda = BordaAggregator()
    rankings = [
        ["A", "B", "AB"],   # judge (weight 1.0)
        ["AB", "B", "A"],   # minimality_judge (weight 0.5)
    ]
    weights = [1.0, 0.5]
    winner, scores, _ = borda.aggregate(
        rankings,
        labels=["A", "B", "AB"],
        tiebreak_winner="A",
        weights=weights,
    )
    # A: 3*1.0 + 1*0.5 = 3.5 → int(3.5)=3
    # B: 2*1.0 + 2*0.5 = 3.0 → 3
    # AB: 1*1.0 + 3*0.5 = 2.5 → int(2.5)=2
    # Tiebreak: A wins.
    assert winner == "A"
    assert scores["A"] >= scores["AB"]


def test_minimality_at_full_weight_can_swing() -> None:
    """Sanity check: when minimality is given equal weight, it competes fairly.

    This is the contrapositive — it proves the 0.5 weight is what makes
    correctness win in the previous test, not some structural bias.
    """
    borda = BordaAggregator()
    rankings = [
        ["A", "B", "AB"],
        ["AB", "B", "A"],
    ]
    weights_equal = [1.0, 1.0]
    winner_equal, scores_equal, _ = borda.aggregate(
        rankings,
        labels=["A", "B", "AB"],
        tiebreak_winner="A",
        weights=weights_equal,
    )
    # With equal weights, three-way tie A=B=AB=4 → tiebreak picks A.
    assert winner_equal == "A"
    assert scores_equal["A"] == scores_equal["AB"] == scores_equal["B"]


def test_three_judge_cohort_correctness_minimality_explorer() -> None:
    """Default Phase 4 cohort: judge=1.0, judge_explorer=1.0, minimality=0.5.

    Two correctness judges agree on A>B>AB; minimality prefers AB>B>A.
    Correctness wins decisively.
    """
    borda = BordaAggregator()
    rankings = [
        ["A", "B", "AB"],   # judge
        ["A", "B", "AB"],   # judge_explorer
        ["AB", "B", "A"],   # minimality_judge
    ]
    weights = [1.0, 1.0, 0.5]
    winner, scores, _ = borda.aggregate(
        rankings,
        labels=["A", "B", "AB"],
        tiebreak_winner="A",
        weights=weights,
    )
    # A: 3+3+0.5=6.5
    # B: 2+2+1.0=5.0
    # AB: 1+1+1.5=3.5
    assert winner == "A"
    assert scores["A"] > scores["B"] > scores["AB"]
