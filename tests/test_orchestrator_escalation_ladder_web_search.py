"""v0.17.0 S2: WEB_SEARCH ladder rung between PIVOT and SOFT_BLOCKER."""

from __future__ import annotations

from orchestrator.escalation_ladder import StuckState, next_step


def test_web_search_fires_at_pivot_2_with_zero_searches() -> None:
    state = StuckState(discard_count=5, pivot_count=2, search_count=0)
    assert next_step(state) == "WEB_SEARCH"


def test_web_search_does_not_fire_below_pivot_threshold() -> None:
    state = StuckState(discard_count=5, pivot_count=1, search_count=0)
    assert next_step(state) == "PIVOT"


def test_web_search_remains_until_budget_exhausted() -> None:
    """Two prior searches still leave budget for one more."""
    state = StuckState(discard_count=5, pivot_count=2, search_count=2)
    assert next_step(state) == "WEB_SEARCH"


def test_search_count_3_promotes_to_soft_blocker() -> None:
    """The 3-per-task cooldown caps autonomous escalation."""
    state = StuckState(discard_count=5, pivot_count=2, search_count=3)
    assert next_step(state) == "SOFT_BLOCKER"


def test_pivot_3_overrides_web_search() -> None:
    """SOFT_BLOCKER beats WEB_SEARCH even with budget remaining."""
    state = StuckState(discard_count=5, pivot_count=3, search_count=0)
    assert next_step(state) == "SOFT_BLOCKER"


def test_default_search_count_is_zero() -> None:
    state = StuckState()
    assert state.search_count == 0
    assert state.last_search_iter == 0
