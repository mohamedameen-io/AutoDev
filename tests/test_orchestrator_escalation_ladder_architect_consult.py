"""v0.26.1 patch G: ARCHITECT_CONSULT escalation rung tests.

The architect-consult rung sits between WEB_SEARCH and SOFT_BLOCKER. After
the web-search budget is exhausted (search_count >= 3) and before falling
through to terminal human handoff, the orchestrator re-delegates to the
architect for diagnose+advise. Mimics human-team behavior: junior dev
struggles → asks the senior architect who designed the plan.

* ARCHITECT_CONSULT is one-shot per task (``architect_count >= 1`` →
  SOFT_BLOCKER on the next escalation).
* SOFT_BLOCKER beats every other rung — the terminal check stays first.
"""

from __future__ import annotations

from orchestrator.escalation_ladder import StuckState, next_step


def test_architect_consult_fires_after_search_count_3() -> None:
    """``search_count >= 3`` and ``architect_count == 0`` → ARCHITECT_CONSULT
    (NOT SOFT_BLOCKER — the architect gets one shot first)."""
    state = StuckState(
        discard_count=5,
        pivot_count=2,
        search_count=3,
        architect_count=0,
    )
    assert next_step(state) == "ARCHITECT_CONSULT"


def test_architect_consult_is_one_shot() -> None:
    """After the architect has been consulted once
    (``architect_count >= 1``), the next escalation falls through to
    SOFT_BLOCKER even though search_count would still match."""
    state = StuckState(
        discard_count=5,
        pivot_count=2,
        search_count=3,
        architect_count=1,
    )
    assert next_step(state) == "SOFT_BLOCKER"


def test_architect_consult_loses_to_pivot_soft_blocker() -> None:
    """``pivot_count >= 3`` still terminates regardless of search_count or
    architect_count — the existing pivot-threshold takes precedence."""
    state = StuckState(
        discard_count=5,
        pivot_count=3,
        search_count=3,
        architect_count=0,
    )
    assert next_step(state) == "SOFT_BLOCKER"


def test_architect_count_default_is_zero() -> None:
    """StuckState defaults are still all-zero (back-compat)."""
    state = StuckState()
    assert state.architect_count == 0


def test_below_search_threshold_still_web_search() -> None:
    """search_count=2 → still WEB_SEARCH (the threshold is 3)."""
    state = StuckState(
        discard_count=5,
        pivot_count=2,
        search_count=2,
        architect_count=0,
    )
    assert next_step(state) == "WEB_SEARCH"
