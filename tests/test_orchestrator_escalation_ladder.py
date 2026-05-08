"""v0.15.0: stuck-recovery escalation ladder.

Pure unit tests for :mod:`orchestrator.escalation_ladder`. The ladder is a
graduated response to repeated discard / pivot signals on the same task:

* ``discard_count`` 0..2  → ``"continue"`` (ordinary retry)
* ``discard_count >= 3``   → ``"REFINE"`` (small-adjustment critic invocation)
* ``discard_count >= 5``   → ``"PIVOT"`` (radical-direction critic invocation)
* ``pivot_count >= 3``     → ``"SOFT_BLOCKER"`` (terminate with human handoff)

Web search step (between PIVOT and SOFT_BLOCKER per leo-lilinxiao) is
deferred to v0.15.1 — see plan section "DEFERRED".
"""

from __future__ import annotations

import pytest

from orchestrator.escalation_ladder import StuckState, next_step


def test_default_state_is_zeroed() -> None:
    state = StuckState()
    assert state.discard_count == 0
    assert state.pivot_count == 0
    assert state.last_event == ""


def test_next_step_continue_when_below_thresholds() -> None:
    state = StuckState(discard_count=0, pivot_count=0)
    assert next_step(state) == "continue"
    state = StuckState(discard_count=1, pivot_count=0)
    assert next_step(state) == "continue"
    state = StuckState(discard_count=2, pivot_count=0)
    assert next_step(state) == "continue"


def test_next_step_refine_at_3_discards() -> None:
    state = StuckState(discard_count=3, pivot_count=0)
    assert next_step(state) == "REFINE"
    state = StuckState(discard_count=4, pivot_count=0)
    assert next_step(state) == "REFINE"


def test_next_step_pivot_at_5_discards() -> None:
    state = StuckState(discard_count=5, pivot_count=0)
    assert next_step(state) == "PIVOT"
    state = StuckState(discard_count=10, pivot_count=0)
    assert next_step(state) == "PIVOT"


def test_next_step_soft_blocker_at_3_pivots() -> None:
    """3 pivots ⇒ SOFT_BLOCKER regardless of discard_count."""
    state = StuckState(discard_count=2, pivot_count=3)
    assert next_step(state) == "SOFT_BLOCKER"
    state = StuckState(discard_count=10, pivot_count=4)
    assert next_step(state) == "SOFT_BLOCKER"


def test_pivot_count_takes_precedence_over_discard_thresholds() -> None:
    """When BOTH a pivot threshold AND a discard threshold qualify,
    ``SOFT_BLOCKER`` (the more terminal step) wins."""
    # 3 pivots AND 5 discards ⇒ SOFT_BLOCKER, not PIVOT.
    state = StuckState(discard_count=5, pivot_count=3)
    assert next_step(state) == "SOFT_BLOCKER"


def test_state_is_a_dataclass_with_named_fields() -> None:
    state = StuckState(discard_count=2, pivot_count=1, last_event="discard")
    assert state.discard_count == 2
    assert state.pivot_count == 1
    assert state.last_event == "discard"


@pytest.mark.parametrize(
    "discards,pivots,expected",
    [
        (0, 0, "continue"),
        (1, 0, "continue"),
        (2, 0, "continue"),
        (3, 0, "REFINE"),
        (4, 0, "REFINE"),
        (5, 0, "PIVOT"),
        (6, 0, "PIVOT"),
        (3, 1, "REFINE"),
        # v0.17.0: pivot_count >= 2 + search_count == 0 → WEB_SEARCH wins.
        # The legacy v0.15.0 expectation here was PIVOT.
        (5, 2, "WEB_SEARCH"),
        (5, 3, "SOFT_BLOCKER"),
        (0, 3, "SOFT_BLOCKER"),
        (0, 4, "SOFT_BLOCKER"),
    ],
)
def test_next_step_table(discards: int, pivots: int, expected: str) -> None:
    state = StuckState(discard_count=discards, pivot_count=pivots)
    assert next_step(state) == expected
