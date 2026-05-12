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


def test_search_count_3_promotes_to_architect_consult() -> None:
    """v0.26.1 patch G: the 3-per-task search cooldown previously
    promoted to SOFT_BLOCKER directly. The new ladder routes through
    ARCHITECT_CONSULT first — the architect gets one shot before the
    task is handed off to the human."""
    state = StuckState(discard_count=5, pivot_count=2, search_count=3)
    assert next_step(state) == "ARCHITECT_CONSULT"


def test_search_count_3_with_architect_already_consulted_promotes_to_soft_blocker() -> None:
    """v0.26.1 patch G: once architect_count >= 1, the ladder exits to
    SOFT_BLOCKER on subsequent escalations — the architect rung is
    one-shot."""
    state = StuckState(
        discard_count=5, pivot_count=2, search_count=3, architect_count=1
    )
    assert next_step(state) == "SOFT_BLOCKER"


def test_pivot_3_overrides_web_search() -> None:
    """SOFT_BLOCKER beats WEB_SEARCH even with budget remaining."""
    state = StuckState(discard_count=5, pivot_count=3, search_count=0)
    assert next_step(state) == "SOFT_BLOCKER"


def test_default_search_count_is_zero() -> None:
    state = StuckState()
    assert state.search_count == 0
    assert state.last_search_iter == 0
