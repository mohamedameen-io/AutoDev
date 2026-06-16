"""v0.22.2 B2: ``PhaseStuckError`` replaces silent FSM stall.

Pre-B2: ``_execute_phase_dag`` returned silently when a phase had
zero pending tasks but tasks were wedged in non-terminal states. The
run looked like a clean completion. Now we surface the stuck task IDs
to the operator so they know to run ``autodev resume`` (which v0.22.2
B1 pairs with a ``reap_orphans`` reset).
"""

from __future__ import annotations


from errors import AutodevError, PhaseStuckError


def test_phase_stuck_error_subclasses_autodev_error() -> None:
    """PhaseStuckError is catchable as AutodevError."""
    err = PhaseStuckError("p1", ["t1"])
    assert isinstance(err, AutodevError)


def test_phase_stuck_error_message_names_phase_and_tasks() -> None:
    err = PhaseStuckError("0", ["0.1", "0.c2", "0.c3"])
    msg = str(err)
    assert "'0'" in msg
    assert "'0.1'" in msg
    assert "'0.c2'" in msg
    assert "'0.c3'" in msg
    # The remediation hint is the contract — operators see how to recover.
    assert "autodev resume" in msg


def test_phase_stuck_error_preserves_fields() -> None:
    err = PhaseStuckError("phase-id", ["a", "b"])
    assert err.phase_id == "phase-id"
    assert err.stuck_task_ids == ["a", "b"]


def test_phase_stuck_error_count_in_message() -> None:
    """Human-readable count of stuck tasks appears in message."""
    err = PhaseStuckError("p", ["x", "y", "z"])
    assert "3 task(s)" in str(err) or "3 tasks" in str(err)
