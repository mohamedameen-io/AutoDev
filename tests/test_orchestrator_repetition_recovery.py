"""v0.32.0 Phase 4.4: repetition-loop recovery action taxonomy tests.

Pure unit tests for :func:`orchestrator.repetition_recovery.choose_recovery_action`.
The decision tree is documented in the module docstring.
"""

from __future__ import annotations

from orchestrator.repetition_recovery import choose_recovery_action


def test_low_discard_picks_switch_tactic() -> None:
    """discard_count <= 2 + repetition_loop ⇒ switch_tactic (cheap path)."""
    assert (
        choose_recovery_action(
            discard_count=1,
            pivot_count=0,
            architect_count=0,
            qa_gates_passed=False,
            repetition_loop_detected=True,
        )
        == "switch_tactic"
    )


def test_discard_3_same_files_picks_increase_scope() -> None:
    """discard_count == 3 + no pivots ⇒ increase_scope (split task)."""
    assert (
        choose_recovery_action(
            discard_count=3,
            pivot_count=0,
            architect_count=0,
            qa_gates_passed=False,
            repetition_loop_detected=True,
        )
        == "increase_scope"
    )


def test_discard_4_no_pivots_picks_re_architect() -> None:
    """discard_count >= 4 + no pivots ⇒ re_architect (structural rethink)."""
    assert (
        choose_recovery_action(
            discard_count=4,
            pivot_count=0,
            architect_count=0,
            qa_gates_passed=False,
            repetition_loop_detected=True,
        )
        == "re_architect"
    )


def test_architect_consulted_picks_ask_human() -> None:
    """architect_count >= 1 ⇒ ask_human (escalation budget exhausted)."""
    assert (
        choose_recovery_action(
            discard_count=2,
            pivot_count=0,
            architect_count=1,
            qa_gates_passed=False,
            repetition_loop_detected=True,
        )
        == "ask_human"
    )


def test_high_discard_picks_ask_human() -> None:
    """discard_count >= 5 ⇒ ask_human even without architect involvement."""
    assert (
        choose_recovery_action(
            discard_count=5,
            pivot_count=0,
            architect_count=0,
            qa_gates_passed=False,
            repetition_loop_detected=False,
        )
        == "ask_human"
    )


def test_qa_passed_with_loop_picks_do_nothing() -> None:
    """QA passed + repetition_loop + discard_count >= 2 ⇒ do_nothing."""
    assert (
        choose_recovery_action(
            discard_count=2,
            pivot_count=0,
            architect_count=0,
            qa_gates_passed=True,
            repetition_loop_detected=True,
        )
        == "do_nothing"
    )


def test_default_falls_to_switch_tactic() -> None:
    """No specific rule fires ⇒ switch_tactic (default safe action)."""
    # discard_count=0 with no loop and no other signals → default.
    assert (
        choose_recovery_action(
            discard_count=0,
            pivot_count=0,
            architect_count=0,
            qa_gates_passed=False,
            repetition_loop_detected=False,
        )
        == "switch_tactic"
    )


def test_qa_passed_without_loop_does_not_pick_do_nothing() -> None:
    """do_nothing requires the repetition_loop signal — QA pass alone isn't enough."""
    # qa_gates_passed=True but no loop → falls through to default.
    assert (
        choose_recovery_action(
            discard_count=2,
            pivot_count=0,
            architect_count=0,
            qa_gates_passed=True,
            repetition_loop_detected=False,
        )
        == "switch_tactic"
    )


def test_qa_passed_low_discard_does_not_pick_do_nothing() -> None:
    """do_nothing requires discard_count >= 2 — first attempt isn't enough."""
    assert (
        choose_recovery_action(
            discard_count=1,
            pivot_count=0,
            architect_count=0,
            qa_gates_passed=True,
            repetition_loop_detected=True,
        )
        == "switch_tactic"
    )


def test_pivot_already_happened_skips_re_architect() -> None:
    """re_architect requires pivot_count == 0 — if we pivoted, fall through."""
    # discard_count=4 + pivot_count=1 → does not fire re_architect.
    # Falls through past increase_scope (also requires pivot_count==0)
    # past the low-discard rule, and lands on switch_tactic default.
    assert (
        choose_recovery_action(
            discard_count=4,
            pivot_count=1,
            architect_count=0,
            qa_gates_passed=False,
            repetition_loop_detected=False,
        )
        == "switch_tactic"
    )


def test_ask_human_takes_priority_over_re_architect() -> None:
    """architect_count >= 1 short-circuits even when discard_count fits re_architect."""
    assert (
        choose_recovery_action(
            discard_count=4,
            pivot_count=0,
            architect_count=1,
            qa_gates_passed=False,
            repetition_loop_detected=False,
        )
        == "ask_human"
    )
