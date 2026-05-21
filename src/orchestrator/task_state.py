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

v0.29.0 Bug 7: ``in_progress`` may also fall to ``quarantined`` on a
typed infrastructure halt (e.g. :class:`AuthenticationFailedError`).
``quarantined`` is non-terminal — :meth:`Orchestrator.resume` picks
quarantined tasks up automatically and walks them back through
``in_progress`` so the rest of the pipeline edges re-apply unchanged.
"""

from __future__ import annotations

from state.schemas import TaskStatus


TASK_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    # v0.26.1 patch G: ``in_progress`` -> ``skipped`` is now allowed so
    # the ARCHITECT_CONSULT "refine-tasks" resolution can supersede a
    # failing task with corrective sub-tasks. Metadata ``architect_consult_action="refine"``
    # distinguishes the architect-driven skip from a user-driven one.
    #
    # v0.29.0 Bug 7: ``in_progress`` -> ``quarantined`` covers the typed
    # halt path (``AuthenticationFailedError`` and other infra failures).
    # ``quarantined`` -> ``in_progress`` is the resume edge: the operator
    # clears the underlying infra issue and ``Orchestrator.resume()``
    # picks the task up again. ``blocked`` -> ``quarantined`` is the
    # operator/auth-recovery upgrade path so an already-blocked task can
    # be re-categorised as resumable instead of terminal.
    "pending": {"in_progress", "skipped", "blocked"},
    "in_progress": {
        "coded",
        "blocked",
        "in_progress",
        "skipped",
        "quarantined",
    },
    # auto-gates retry back to in_progress on failure
    # v0.37.0 H3: also allow ``coded``/``auto_gated``/``reviewed``/
    # ``tested`` → ``quarantined`` so a typed infrastructure halt that
    # raises mid-pipeline (e.g. the test-diag circuit breaker tripping
    # while the task is in the ``reviewed`` slot waiting on the test-
    # engineer call) can stamp the offending task as resumable rather
    # than silently failing the transition and leaving the phase
    # un-paused. ``tournamented`` already terminates so adding the edge
    # there would only matter if a tournament-stage typed halt surfaces;
    # included for symmetry and forward-compat.
    "coded": {"auto_gated", "in_progress", "blocked", "quarantined"},
    "auto_gated": {"reviewed", "in_progress", "blocked", "quarantined"},
    "reviewed": {"tested", "in_progress", "blocked", "quarantined"},
    "tested": {"tournamented", "in_progress", "blocked", "quarantined"},
    "tournamented": {"complete", "blocked"},
    "complete": set(),
    # Blocked tasks can only be moved back to in_progress by an explicit
    # resume decision. v0.29.0 Bug 7: also allow promotion to
    # ``quarantined`` so an operator can upgrade an infra-flavoured
    # block to the resumable surface.
    "blocked": {"in_progress", "quarantined"},
    "skipped": set(),
    # v0.29.0 Bug 7: non-terminal infrastructure-halt state. Resume goes
    # back through ``in_progress`` so the rest of the pipeline edges
    # (coded/auto_gated/...) re-apply unchanged.
    "quarantined": {"in_progress"},
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
