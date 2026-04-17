"""Tests for :mod:`orchestrator.task_state`."""

from __future__ import annotations

import pytest

from orchestrator.task_state import (
    TASK_TRANSITIONS,
    assert_transition,
    can_transition,
)


VALID_TRANSITIONS = [
    ("pending", "in_progress"),
    ("pending", "skipped"),
    ("pending", "blocked"),
    ("in_progress", "coded"),
    ("in_progress", "blocked"),
    ("in_progress", "in_progress"),  # explicit self-loop for retry bookkeeping
    ("coded", "auto_gated"),
    ("coded", "in_progress"),
    ("coded", "blocked"),
    ("auto_gated", "reviewed"),
    ("auto_gated", "in_progress"),
    ("reviewed", "tested"),
    ("reviewed", "in_progress"),
    ("tested", "tournamented"),
    ("tested", "in_progress"),
    ("tournamented", "complete"),
    ("tournamented", "blocked"),
    ("blocked", "in_progress"),
]


INVALID_TRANSITIONS = [
    ("pending", "coded"),
    ("pending", "complete"),
    ("pending", "tested"),
    ("complete", "in_progress"),
    ("complete", "blocked"),
    ("skipped", "in_progress"),
    ("coded", "tested"),  # must go through auto_gated
    ("in_progress", "complete"),
    ("reviewed", "complete"),
    ("pending", "pending"),  # self-loop not allowed unless explicit
    ("complete", "complete"),
]


@pytest.mark.parametrize("from_,to", VALID_TRANSITIONS)
def test_valid_transitions_allowed(from_: str, to: str) -> None:
    assert can_transition(from_, to), f"{from_} -> {to} should be allowed"
    assert_transition(from_, to)  # no raise


@pytest.mark.parametrize("from_,to", INVALID_TRANSITIONS)
def test_invalid_transitions_raise(from_: str, to: str) -> None:
    assert not can_transition(from_, to)
    with pytest.raises(ValueError) as excinfo:
        assert_transition(from_, to)
    msg = str(excinfo.value)
    assert from_ in msg
    assert to in msg


def test_terminal_states_have_no_outgoing_transitions() -> None:
    assert TASK_TRANSITIONS["complete"] == set()
    assert TASK_TRANSITIONS["skipped"] == set()


def test_every_status_is_a_key() -> None:
    """Every status used as a source in transitions also appears as a key."""
    all_used = set(TASK_TRANSITIONS)
    for targets in TASK_TRANSITIONS.values():
        all_used.update(targets)
    assert all_used == set(TASK_TRANSITIONS)
