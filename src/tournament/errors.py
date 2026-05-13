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


__all__ = ["AuthenticationFailedError"]
