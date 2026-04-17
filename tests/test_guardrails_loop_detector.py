"""Tests for :class:`src.guardrails.loop_detector.LoopDetector`."""

from __future__ import annotations

import pytest

from errors import GuardrailExceededError
from guardrails.loop_detector import LoopDetector


def test_no_loop_with_unique_outputs() -> None:
    ld = LoopDetector(window=3, threshold=2)
    ld.observe("t1", "developer", "output A")
    ld.observe("t1", "developer", "output B")
    ld.observe("t1", "developer", "output C")
    # No exception — all unique.


def test_loop_detected_at_threshold() -> None:
    ld = LoopDetector(window=3, threshold=2)
    ld.observe("t1", "developer", "same output")
    ld.observe("t1", "developer", "different")
    with pytest.raises(GuardrailExceededError, match="loop detected for task t1"):
        ld.observe("t1", "developer", "same output")


def test_loop_detected_all_same() -> None:
    ld = LoopDetector(window=3, threshold=2)
    ld.observe("t1", "developer", "same")
    ld.observe("t1", "developer", "same")
    # Window not yet full (only 2 entries, window=3) — no raise yet.
    # Third observation fills window and triggers.
    with pytest.raises(GuardrailExceededError):
        ld.observe("t1", "developer", "same")


def test_window_slides() -> None:
    """Old entries fall out of the window."""
    ld = LoopDetector(window=3, threshold=2)
    ld.observe("t1", "developer", "same")
    ld.observe("t1", "developer", "same")
    # Window: [same, same] — not yet full (window=3).
    ld.observe("t1", "developer", "different A")
    # Window: [same, same, different A] — most recent is "different A", count=1. No raise.
    ld.observe("t1", "developer", "different B")
    # Window: [same, different A, different B] — most recent is "different B", count=1. No raise.


def test_different_roles_are_independent() -> None:
    """Different roles on the same task have separate history."""
    ld = LoopDetector(window=3, threshold=2)
    # Coder and reviewer both return "same" — but tracked separately.
    ld.observe("t1", "developer", "same")
    ld.observe("t1", "reviewer", "same")
    ld.observe("t1", "developer", "same")
    ld.observe("t1", "reviewer", "same")
    # Neither has reached window=3 yet — no raise.
    # Coder now has 3 identical entries → raises.
    with pytest.raises(GuardrailExceededError, match="task t1"):
        ld.observe("t1", "developer", "same")
    # Reviewer also has 3 identical entries → raises independently.
    with pytest.raises(GuardrailExceededError, match="task t1"):
        ld.observe("t1", "reviewer", "same")


def test_different_roles_trigger_independently() -> None:
    ld = LoopDetector(window=3, threshold=2)
    ld.observe("t1", "developer", "same")
    ld.observe("t1", "developer", "same")
    with pytest.raises(GuardrailExceededError, match="task t1"):
        ld.observe("t1", "developer", "same")
    # Reviewer has no history — no raise.
    ld.observe("t1", "reviewer", "same")
    ld.observe("t1", "reviewer", "same")


def test_different_task_ids_are_independent() -> None:
    ld = LoopDetector(window=3, threshold=2)
    ld.observe("t1", "developer", "same")
    ld.observe("t1", "developer", "same")
    # t2 has its own history — no raise.
    ld.observe("t2", "developer", "same")
    ld.observe("t2", "developer", "same")
    # t1 now triggers.
    with pytest.raises(GuardrailExceededError, match="task t1"):
        ld.observe("t1", "developer", "same")


def test_reset_clears_history() -> None:
    ld = LoopDetector(window=3, threshold=2)
    ld.observe("t1", "developer", "same")
    ld.observe("t1", "developer", "same")
    ld.reset("t1")
    # After reset, history is cleared — no raise.
    ld.observe("t1", "developer", "same")
    ld.observe("t1", "developer", "same")
    # Window not full yet (only 2 entries after reset).


def test_reset_clears_all_roles() -> None:
    ld = LoopDetector(window=3, threshold=2)
    ld.observe("t1", "developer", "x")
    ld.observe("t1", "reviewer", "y")
    assert ld.is_tracking("t1")
    ld.reset("t1")
    assert not ld.is_tracking("t1")


def test_is_tracking() -> None:
    ld = LoopDetector(window=3, threshold=2)
    assert not ld.is_tracking("t1")
    ld.observe("t1", "developer", "hello")
    assert ld.is_tracking("t1")
    ld.reset("t1")
    assert not ld.is_tracking("t1")


def test_invalid_window_raises() -> None:
    with pytest.raises(ValueError, match="window must be >= 1"):
        LoopDetector(window=0)


def test_invalid_threshold_raises() -> None:
    with pytest.raises(ValueError, match="threshold must be between"):
        LoopDetector(window=3, threshold=4)


def test_threshold_equals_window() -> None:
    """threshold == window is valid: all entries must match."""
    ld = LoopDetector(window=2, threshold=2)
    ld.observe("t1", "developer", "same")
    with pytest.raises(GuardrailExceededError):
        ld.observe("t1", "developer", "same")


def test_error_message_contains_count_and_window() -> None:
    ld = LoopDetector(window=3, threshold=2)
    ld.observe("t1", "developer", "repeat")
    ld.observe("t1", "developer", "other")
    with pytest.raises(GuardrailExceededError, match=r"\d+/3"):
        ld.observe("t1", "developer", "repeat")
