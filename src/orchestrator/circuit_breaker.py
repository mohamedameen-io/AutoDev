"""v0.30.0 Bug 5: cross-task infrastructure-failure circuit breaker.

Counts adapter failures whose ``subtype`` falls into the *infrastructure*
class — ``auth_failed``, ``rate_limited``, ``server_error`` — and trips
when the rolling-window count reaches a threshold. The orchestrator's
``delegate()`` site feeds every adapter result into the breaker; on a
trip the breaker raises :class:`tournament.errors.InfrastructureCircuitOpenError`
which the existing :class:`AuthenticationFailedError` catch sites
(v0.29.0 Bug 7) treat identically — quarantine the in-flight task,
park the phase at ``review_status="paused"``, surface a non-zero exit.

Why a *cross-task* breaker on top of the per-task
:class:`AuthenticationFailedError` halt: a single bad token already
trips the v0.28.0 path on the first adapter call. The breaker covers
the broader class — a flaky 5xx burst from the upstream API or an
intermittent rate-limit storm produces ``auth_failed`` /
``server_error`` / ``rate_limited`` subtypes across multiple tasks
before any single task accumulates enough retries to halt itself.
Three of those across the run (default) is enough signal that
continuing will just thrash; the breaker halts the whole run with an
actionable message.

Subtypes that DO NOT count toward the trip:

* ``client_error`` — typically 4xx-class non-auth (bad request, malformed
  payload). A per-task verdict, not an infrastructure signal.
* ``error_max_turns`` / ``error_max_tokens`` — agent legitimately
  exhausted its budget; not an infrastructure problem.
* ``None`` — adapter returned no subtype; treat as "unknown verdict",
  not an infrastructure failure.

Design choices:

* Time backend is :class:`datetime.datetime` (UTC-aware) to match the
  orchestrator's existing time handling in
  :func:`execute_phase._compute_retry_delay_s` (which uses
  ``datetime.now(timezone.utc)`` + ``datetime.fromisoformat``). The
  breaker is fed timestamps explicitly so tests can drive it with
  fixed clocks and the production caller passes ``datetime.utcnow()``
  at the call site.
* Storage is a :class:`collections.deque` of timestamps. Eviction
  happens lazily inside :meth:`should_halt` — entries older than the
  rolling window are popped from the left. This keeps
  :meth:`record_failure` O(1) amortized and bounds memory at the
  threshold's worth of recent infra failures (deque maxlen would
  truncate silently; the explicit eviction is preferable for the
  message-building path).
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Final


# Subtypes that count toward an infrastructure-class trip. Kept module-
# level (not a class attribute) so it's importable for cross-checks
# from the adapter / classifier layer if ever needed.
INFRASTRUCTURE_SUBTYPES: Final[frozenset[str]] = frozenset(
    {
        "auth_failed",
        "rate_limited",
        "server_error",
    }
)


class InfraFailureCircuitBreaker:
    """Rolling-window counter for adapter infrastructure failures.

    See module docstring for the full rationale.

    :param threshold: Number of infrastructure failures within the
        rolling window that opens the breaker. Default 3 mirrors the
        ``cfg.circuit_breaker_threshold`` default.
    :param window_s: Rolling window in seconds. Default 60.0 mirrors
        the ``cfg.circuit_breaker_window_s`` default.
    """

    def __init__(self, threshold: int = 3, window_s: float = 60.0) -> None:
        self._threshold = threshold
        self._window_s = window_s
        # Stored as (task_id, subtype, timestamp) so a future
        # ``recent_failures()`` accessor can surface forensics without
        # changing the trip semantics. Eviction is lazy, in
        # ``should_halt`` — keeps ``record_failure`` O(1).
        self._failures: deque[tuple[str, str, datetime]] = deque()

    @property
    def threshold(self) -> int:
        return self._threshold

    @property
    def window_s(self) -> float:
        return self._window_s

    def record_failure(
        self, task_id: str, subtype: str | None, ts: datetime
    ) -> None:
        """Record an adapter failure if its subtype is infrastructure-class.

        No-op for non-infra subtypes (``client_error``, ``error_max_turns``,
        ``None``, anything not in :data:`INFRASTRUCTURE_SUBTYPES`). The
        breaker is *failure-only* — successes go through :meth:`reset`,
        which zeroes the counter rather than appending a "non-failure"
        marker.

        ``ts`` is taken explicitly so the orchestrator passes a single
        ``datetime.utcnow()`` per call site and tests can drive the
        breaker with a fixed clock.
        """
        if subtype is None or subtype not in INFRASTRUCTURE_SUBTYPES:
            return
        # Normalize to UTC-aware so window math is consistent regardless
        # of whether the caller passed a naive UTC stamp (``datetime.utcnow()``)
        # or an aware one (``datetime.now(timezone.utc)``).
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        self._failures.append((task_id, subtype, ts))

    def should_halt(self) -> tuple[bool, str | None]:
        """Return ``(True, reason)`` if the trip threshold is met.

        Lazy-evicts entries older than the rolling window relative to
        the most recent failure. Returns a reason string suitable for
        operator-facing messages (mentions the count and window
        explicitly). When closed, returns ``(False, None)``.
        """
        if not self._failures:
            return (False, None)
        # The "now" for window math is the most recent failure's
        # timestamp — NOT real-time. This means a long pause between
        # bursts doesn't artificially expire prior failures we already
        # judged safe; the next failure that arrives is what re-runs the
        # check and triggers eviction relative to its own timestamp.
        most_recent_ts = self._failures[-1][2]
        cutoff = most_recent_ts - timedelta(seconds=self._window_s)
        # Pop from the left until the oldest entry is inside the window.
        while self._failures and self._failures[0][2] < cutoff:
            self._failures.popleft()

        count = len(self._failures)
        if count >= self._threshold:
            return (
                True,
                (
                    f"infrastructure circuit open — {count} failures in "
                    f"{self._window_s:.0f} seconds. Refresh credentials "
                    f"and `autodev resume`."
                ),
            )
        return (False, None)

    def reset(self) -> None:
        """Zero the counters. Called after every successful adapter result.

        Intentionally cheap and idempotent — the orchestrator calls this
        on every ``result.success=True`` path without checking whether
        the breaker has prior state.
        """
        self._failures.clear()


__all__ = ["InfraFailureCircuitBreaker", "INFRASTRUCTURE_SUBTYPES"]
