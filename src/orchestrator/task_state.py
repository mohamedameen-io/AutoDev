"""Finite-state machine over :data:`state.schemas.TaskStatus`.

States a task moves through inside the execute loop:

    pending
      -> in_progress            # coder assigned
      -> coded                  # coder finished
      -> auto_gated             # QA gates passed (Phase 8)
      -> reviewed               # reviewer APPROVED
      -> tested                 # test_engineer produced evidence
      -> tournamented           # implementation tournament finished (Phase 7)
      -> complete               # task done

Any in-flight state may fall back to ``in_progress`` on retry, or to
``blocked`` on hard failure. ``skipped`` is a user-driven escape hatch.
"""

from __future__ import annotations

from state.schemas import TaskStatus


TASK_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    "pending": {"in_progress", "skipped", "blocked"},
    "in_progress": {"coded", "blocked", "in_progress"},
    # auto-gates retry back to in_progress on failure
    "coded": {"auto_gated", "in_progress", "blocked"},
    "auto_gated": {"reviewed", "in_progress", "blocked"},
    "reviewed": {"tested", "in_progress", "blocked"},
    "tested": {"tournamented", "in_progress", "blocked"},
    "tournamented": {"complete", "blocked"},
    "complete": set(),
    # Blocked tasks can only be moved back to in_progress by an explicit
    # resume decision.
    "blocked": {"in_progress"},
    "skipped": set(),
}


def can_transition(from_: TaskStatus, to: TaskStatus) -> bool:
    """Return True iff ``from_ -> to`` is in :data:`TASK_TRANSITIONS`.

    Self-loops are allowed only when explicitly listed (``in_progress ->
    in_progress``) so tests can drive retry bookkeeping without changing
    status.
    """
    if from_ == to and to in TASK_TRANSITIONS.get(from_, set()):
        return True
    if from_ == to:
        return False
    return to in TASK_TRANSITIONS.get(from_, set())


def assert_transition(from_: TaskStatus, to: TaskStatus) -> None:
    """Raise :class:`ValueError` if the transition is not allowed."""
    if not can_transition(from_, to):
        allowed = sorted(TASK_TRANSITIONS.get(from_, set()))
        raise ValueError(
            f"invalid task transition {from_!r} -> {to!r}; "
            f"allowed from {from_!r}: {allowed}"
        )


__all__ = [
    "TASK_TRANSITIONS",
    "assert_transition",
    "can_transition",
]
