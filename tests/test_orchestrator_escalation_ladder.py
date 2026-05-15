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


# ---------------------------------------------------------------------------
# v0.32.0 Phase 4.1: knowledge-aware ladder gating.
# ---------------------------------------------------------------------------


def test_repetition_loop_forces_pivot_at_low_discard_count() -> None:
    """``repetition_loop`` in patterns ⇒ baseline ``continue`` becomes ``PIVOT``."""
    state = StuckState(discard_count=1, pivot_count=0)
    # Baseline (no patterns) is ``continue``.
    assert next_step(state) == "continue"
    # With ``repetition_loop`` detected, the ladder forces ``PIVOT``.
    knowledge_context = {"detected_patterns": ["repetition_loop"]}
    assert next_step(state, knowledge_context=knowledge_context) == "PIVOT"


def test_repetition_loop_overrides_refine_to_pivot() -> None:
    """At discard_count=3 (REFINE baseline), repetition_loop bumps to PIVOT."""
    state = StuckState(discard_count=3, pivot_count=0)
    assert next_step(state) == "REFINE"
    knowledge_context = {"detected_patterns": ["repetition_loop"]}
    assert next_step(state, knowledge_context=knowledge_context) == "PIVOT"


def test_no_pattern_passes_through_unchanged() -> None:
    """Backward-compat: knowledge_context=None preserves pre-Phase-4 behaviour."""
    state = StuckState(discard_count=3, pivot_count=0)
    assert next_step(state, knowledge_context=None) == "REFINE"
    # Empty dict is also a no-op.
    assert next_step(state, knowledge_context={}) == "REFINE"
    # Empty patterns list is also a no-op.
    assert next_step(state, knowledge_context={"detected_patterns": []}) == "REFINE"


def test_unrecognised_pattern_is_ignored() -> None:
    """A pattern name not in the override set leaves the ladder untouched."""
    state = StuckState(discard_count=3, pivot_count=0)
    knowledge_context = {"detected_patterns": ["expansion_drift", "stuck_on_test"]}
    # Neither ``expansion_drift`` nor ``stuck_on_test`` triggers an
    # override — the ladder's PRM-aware path is intentionally narrow.
    assert next_step(state, knowledge_context=knowledge_context) == "REFINE"


def test_ping_pong_escalates_refine_to_architect_consult() -> None:
    """``ping_pong`` at REFINE baseline routes to ARCHITECT_CONSULT."""
    state = StuckState(discard_count=3, pivot_count=0, architect_count=0)
    knowledge_context = {"detected_patterns": ["ping_pong"]}
    assert next_step(state, knowledge_context=knowledge_context) == "ARCHITECT_CONSULT"


def test_ping_pong_after_architect_routes_to_soft_blocker() -> None:
    """When the architect already weighed in, ping_pong escalates to SOFT_BLOCKER."""
    state = StuckState(discard_count=3, pivot_count=0, architect_count=1)
    # Even before the override, architect_count >= 1 already lands on SOFT_BLOCKER —
    # confirm the patterned path agrees.
    knowledge_context = {"detected_patterns": ["ping_pong"]}
    assert next_step(state, knowledge_context=knowledge_context) == "SOFT_BLOCKER"


def test_repetition_loop_does_not_downgrade_pivot() -> None:
    """A baseline of PIVOT or higher is not affected by repetition_loop override."""
    state = StuckState(discard_count=5, pivot_count=0)
    knowledge_context = {"detected_patterns": ["repetition_loop"]}
    # Baseline is PIVOT; override targets continue/REFINE only.
    assert next_step(state, knowledge_context=knowledge_context) == "PIVOT"


def test_malformed_knowledge_context_is_safe() -> None:
    """A malformed ``detected_patterns`` value falls through silently."""
    state = StuckState(discard_count=3, pivot_count=0)
    # ``detected_patterns`` is a non-iterable — must not crash.
    assert (
        next_step(state, knowledge_context={"detected_patterns": 42}) == "REFINE"
    )
