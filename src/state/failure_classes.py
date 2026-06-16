"""v0.32.0 Phase 1.3: plan-time vs execute-time failure-class taxonomy.

The orchestrator has historically conflated two distinct failure modes
under a single set of subtype strings on :class:`AgentResult`. The two
modes have different recovery contracts:

* **Execute-time** failures fire while a worker is iterating on a task
  diff: ``error_max_turns``, ``timeout``, ``parse_error``, ``rate_limited``,
  ``auth_failed``. Recovery is the per-task budget escalator + the
  retry / circuit-breaker plumbing in
  :func:`orchestrator.execute_phase.delegate`.

* **Plan-time** failures fire INSIDE :func:`orchestrator.plan_phase.run_plan_phase`
  while the architect is drafting a plan: a path the validator rejects
  three times in a row, the architect's markdown failing to parse three
  times in a row, the architect repeatedly timing out. Recovery is the
  architect-retry loop with rejection-history feedback (Phase 1.1) +
  budget escalation on the architect's plan-phase scope (Phase 1.2) +
  scope degradation / model escalation (Phase 1.4).

This module defines a small taxonomy that lets the plan-phase recovery
machinery (Phase 1.4) route on a single typed value rather than a
nested ``isinstance`` chain. The :class:`FailureClass` enum is the
single source of truth; :func:`classify` maps an exception OR an
``AgentResult``-like object onto it.

The taxonomy intentionally stays small. New entries should be added
when a recovery branch needs to disambiguate two failures that today
collapse to ``UNKNOWN``; otherwise this module risks growing into a
parallel exception hierarchy nobody reads.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


# Recurrence threshold above which a repeated plan-time failure is
# promoted into its ``*Recurrence`` failure class. Mirrors
# :data:`orchestrator.plan_phase._DROP_AT_RECURRENCE` so the two
# subsystems agree on what counts as "the architect keeps making the
# same mistake".
_RECURRENCE_PROMOTION_THRESHOLD: int = 3


class FailureClass(str, Enum):
    """Typed taxonomy of orchestrator failure modes.

    Backed by :class:`str` so the enum is JSON-friendly when written
    into a ledger op payload: ``payload["failure_class"] =
    FailureClass.PathValidationRecurrence.value``.

    Three branches:

    * ``ExecuteTime*``: per-task adapter failures during execute phase.
      The per-(task_id, role) budget escalator and circuit breaker own
      recovery for these.
    * ``PlanTime*``: architect-convergence failures inside
      :func:`orchestrator.plan_phase.run_plan_phase`. The architect
      retry loop + plan-phase recovery tiers own these.
    * ``Infrastructure*``: cross-cutting environment failures
      (worktree provisioning, indexing) that are neither per-task nor
      plan-time.
    * ``Unknown``: catch-all for failures the classifier can't route.
      Callers should treat ``Unknown`` as "fall through to legacy
      retry / hard-fail" rather than triggering specialised recovery.
    """

    # ----- Execute-time -----
    ErrorMaxTurns = "execute.error_max_turns"
    Timeout = "execute.timeout"
    ParseError = "execute.parse_error"
    RateLimited = "execute.rate_limited"
    AuthFailed = "execute.auth_failed"

    # ----- Plan-time -----
    # A path the architect emitted has been rejected by the validator
    # ``_RECURRENCE_PROMOTION_THRESHOLD`` times across consecutive
    # attempts. Promotes to scope degradation in Phase 1.4 Tier 4.
    PathValidationRecurrence = "plan.path_validation_recurrence"
    # The architect's markdown has failed :class:`PlanParseError`
    # ``_RECURRENCE_PROMOTION_THRESHOLD`` times. Promotes to model
    # escalation in Phase 1.4 Tier 5.
    ParseErrorRecurrence = "plan.parse_error_recurrence"
    # The architect has hit ``error_max_turns`` repeatedly during plan
    # drafting. Promotes the plan-phase budget escalator (Phase 1.2)
    # to its top tier and then to model escalation.
    TimeoutRecurrence = "plan.timeout_recurrence"
    # Single-shot plan parse error (recurrence below threshold).
    PlanParseError = "plan.plan_parse_error"
    # The plan parsed but tripped a structural invariant in
    # :class:`pydantic.ValidationError` — a separate route from
    # :class:`PlanParseError` because the recovery hint differs.
    PlanStructureError = "plan.plan_structure_error"

    # ----- Infrastructure -----
    WorktreeFailure = "infra.worktree_failure"
    IndexingFailure = "infra.indexing_failure"

    # ----- Catch-all -----
    Unknown = "unknown"


def classify(
    exception_or_result: Any,
    *,
    recurrence_count: int = 0,
) -> FailureClass:
    """Map an exception OR an ``AgentResult``-like object onto a class.

    The classifier intentionally accepts a loose ``Any`` signature: the
    call sites pass a raw ``Exception`` (when the plan-phase architect
    retry loop catches a structural failure) OR a raw ``AgentResult``
    (when the execute-phase delegate site wants to typed-route the
    adapter's ``subtype``). Returning :data:`FailureClass.Unknown` for
    inputs the classifier can't route is the safe fallback — callers
    treat it as "use the legacy retry path".

    ``recurrence_count`` is the count of how many times THIS particular
    ``(raw, reason)`` pair has been observed across architect attempts.
    When >= :data:`_RECURRENCE_PROMOTION_THRESHOLD`, plan-time errors
    promote into their ``*Recurrence`` variants — that's the signal
    Phase 1.4's recovery tiers gate on.

    Examples:
        >>> from orchestrator.path_validator import PathValidationError
        >>> err = PathValidationError("notes", "missing_on_disk")
        >>> classify(err, recurrence_count=3)
        <FailureClass.PathValidationRecurrence: 'plan.path_validation_recurrence'>
        >>> classify(err, recurrence_count=1)
        <FailureClass.Unknown: 'unknown'>
    """
    # Defer the import to avoid a module-level cycle: path_validator
    # already imports from state, so importing it at module load would
    # round-trip.
    try:
        from orchestrator.path_validator import PathValidationError  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — defensive against test-time stub envs
        PathValidationError = None  # type: ignore[misc,assignment]

    try:
        from orchestrator.plan_parser import PlanParseError  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        PlanParseError = None  # type: ignore[misc,assignment]

    try:
        from pydantic import ValidationError as _PydValidationError  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        _PydValidationError = None  # type: ignore[misc,assignment]

    # ----- Plan-time exceptions -----
    if PathValidationError is not None and isinstance(
        exception_or_result, PathValidationError
    ):
        if recurrence_count >= _RECURRENCE_PROMOTION_THRESHOLD:
            return FailureClass.PathValidationRecurrence
        # Single-shot path validation failures route through Unknown so
        # the legacy retry path handles them; only the recurrent shape
        # warrants a specialised recovery tier.
        return FailureClass.Unknown

    if PlanParseError is not None and isinstance(
        exception_or_result, PlanParseError
    ):
        if recurrence_count >= _RECURRENCE_PROMOTION_THRESHOLD:
            return FailureClass.ParseErrorRecurrence
        return FailureClass.PlanParseError

    if _PydValidationError is not None and isinstance(
        exception_or_result, _PydValidationError
    ):
        return FailureClass.PlanStructureError

    # ----- Execute-time AgentResult-like objects -----
    subtype = getattr(exception_or_result, "subtype", None)
    if isinstance(subtype, str):
        if subtype == "error_max_turns":
            return FailureClass.ErrorMaxTurns
        if subtype == "timeout":
            return FailureClass.Timeout
        if subtype == "parse_error":
            return FailureClass.ParseError
        if subtype == "rate_limited":
            return FailureClass.RateLimited
        if subtype == "auth_failed":
            return FailureClass.AuthFailed

    # ----- Bare strings (shorthand call sites) -----
    if isinstance(exception_or_result, str):
        for fc in FailureClass:
            if exception_or_result == fc.value:
                return fc

    return FailureClass.Unknown


__all__ = [
    "FailureClass",
    "classify",
]
