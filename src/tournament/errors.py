"""Tournament-engine typed exception hierarchy.

Co-located with :mod:`tournament` (rather than the top-level
:mod:`errors` module) because these subclasses encode tournament-only
recovery semantics that the orchestrator's top-level loop catches by
type — keeping them in the same package as the raise sites makes the
contract surface obvious.

The base :class:`~errors.TournamentError` lives in :mod:`errors` (the
shared autodev hierarchy); we subclass it so existing
``except TournamentError`` handlers still match every typed flavor.
"""

from __future__ import annotations

from errors import TournamentError


class AuthenticationFailedError(TournamentError):
    """Raised when the tournament's adapter call fails with
    ``subtype="auth_failed"`` (401 / 403 from the upstream API).

    The orchestrator's top-level loop in :mod:`orchestrator.execute_phase`
    catches this typed exception, marks the in-flight task as
    ``blocked`` with a structured ``blocked_reason``, and aborts the
    phase loop with a non-zero exit. Distinct from the generic
    :class:`~errors.TournamentError` (which represents a non-retryable
    tournament-engine failure) because auth-class failures imply the
    operator must intervene before any further task can succeed —
    continuing the loop would just thrash every subsequent adapter
    call against the same dead credential.

    Bug 7 in v0.29.0 will replace the ``blocked`` mark with a new
    ``quarantined`` task state so the halt becomes resumable; for
    v0.28.0 the typed-exception contract above is the load-bearing
    surface.
    """


class InfrastructureCircuitOpenError(TournamentError):
    """Raised when the cross-task infrastructure-failure circuit
    breaker (:class:`orchestrator.circuit_breaker.InfraFailureCircuitBreaker`)
    trips — i.e. ``threshold`` or more adapter failures with an
    infrastructure-class subtype (``auth_failed`` / ``rate_limited`` /
    ``server_error``) occurred within the rolling ``window_s`` window.

    Caught at the same top-level sites as
    :class:`AuthenticationFailedError` (added in v0.29.0 Bug 7) and
    treated identically: the in-flight task is stamped
    ``quarantined``, the owning phase is parked at
    ``review_status="paused"``, and the run aborts non-zero with an
    operator-facing message.

    The relationship to :class:`AuthenticationFailedError` is one of
    *generalization*. ``AuthenticationFailedError`` halts on the FIRST
    ``auth_failed`` subtype — which catches a single bad token before
    it cascades. ``InfrastructureCircuitOpenError`` covers the broader
    failure class: a flaky 5xx burst from the upstream API or an
    intermittent rate-limit storm produces ``server_error`` /
    ``rate_limited`` subtypes spread across multiple tasks before any
    single task accumulates enough retries to fail itself. The breaker
    halts the whole run once that pattern crosses a threshold rather
    than letting it thrash every queued task against the same flaky
    backend. A single bad token still trips the breaker via repeated
    ``auth_failed`` events, but the v0.28.0 single-shot path catches
    that case sooner.

    v0.38.0 I4 (HK7): the optional ``halted_task_id`` carries the
    in-flight task id from the raise site to the typed-halt handler
    (:func:`orchestrator.execute_phase._halt_for_auth_failed`). Pre-I4
    that handler walked the plan and inferred the halted task from
    its status — fine when only one task was in flight, but on the
    parallel pool the inference would race the worker stamp and
    sometimes attribute the halt to the wrong task. Passing the id
    explicitly removes the race; the handler still falls back to the
    plan-walk lookup for legacy callers that haven't been migrated.
    """

    def __init__(
        self,
        *args: object,
        halted_task_id: str | None = None,
    ) -> None:
        super().__init__(*args)
        # v0.38.0 I4 (HK7): explicit identity across the
        # raise→catch→halt boundary. ``None`` preserves the legacy
        # plan-walk fallback in ``_halt_for_auth_failed``.
        self.halted_task_id = halted_task_id


__all__ = ["AuthenticationFailedError", "InfrastructureCircuitOpenError"]
