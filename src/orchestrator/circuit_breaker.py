"""v0.30.0 Bug 5: cross-task infrastructure-failure circuit breaker.

Counts adapter failures whose ``subtype`` falls into the *infrastructure*
class — ``auth_failed``, ``rate_limited``, ``server_error`` — and trips
when the rolling-window count reaches a threshold. The orchestrator's
``delegate()`` site feeds every adapter result into the breaker; on a
trip the breaker raises :class:`tournament.errors.InfrastructureCircuitOpenError`
which the existing :class:`AuthenticationFailedError` catch sites
(v0.29.0 Bug 7) treat identically — quarantine the in-flight task,
park the phase at ``review_status="paused"``, surface a non-zero exit.

v0.37.0 Phase H3 extension — *test-diagnosis* stream:
A SECOND independent rolling counter tracks test-runner self-diagnoses
(``capture_failed`` by default) fed by ``execute_phase`` whenever the
test-engineer leg returns an infrastructure-class
:class:`~orchestrator.test_result_classifier.TestDiagnosis`. Real-world
operator runs surfaced a cascading pattern where many distinct tasks
each produced a single ``capture_failed`` event (empty stdout, null
returncode); each retried once and hard-failed in isolation, but no
cross-task signal ever halted the run. The two streams use separate
deques and separate thresholds so adapter-class flakes and test-runner
flakes are diagnosed and tuned independently. ``record_test_diagnosis``
is a strict no-op for diagnoses outside the configured set, mirroring
the ``record_failure`` non-infra subtype branch.

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
        # v0.31.0 (Phase 2.6): Cursor usage-cap hit. Distinguished from
        # ``rate_limited`` (per-minute throttle) because the remediation
        # is different — a usage cap means "the account ran out of
        # monthly credits," and continued retries will keep tripping
        # the same wall. The cursor adapter already downshifts to
        # ``--model auto`` once per call; the breaker's job is to halt
        # the run when even the downshifted retries keep failing with
        # this subtype across multiple tasks.
        "usage_limit_hit",
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
    :param test_diag_threshold: v0.37.0 H3 — separate threshold for the
        test-diagnosis stream (default 3, mirrors
        ``cfg.test_diag_breaker_threshold``). Independent of
        ``threshold`` so adapter-class and test-runner flakes are tuned
        per-stream.
    :param test_diag_window_s: v0.37.0 H3 — rolling window for the
        test-diagnosis stream (default 600s = 10 minutes, mirrors
        ``cfg.test_diag_breaker_window_s``). Wider than the adapter
        window because test runs are slower per attempt.
    :param test_diag_diagnoses: v0.37.0 H3 — which
        :class:`~orchestrator.test_result_classifier.TestDiagnosis`
        values count toward this stream. Defaults to
        ``frozenset({"capture_failed"})``; pass a wider set (e.g.
        adding ``"runtime_crash"``) to also count those.
    :param test_diag_backoff_initial_s: v0.38.0 I4 — first backoff
        delay (seconds) after the test-diag threshold crosses.
        Subsequent crossings grow by
        ``test_diag_backoff_multiplier`` per occurrence, capped at
        ``test_diag_backoff_max_s``.
    :param test_diag_backoff_multiplier: v0.38.0 I4 — exponential
        growth factor between successive backoffs.
    :param test_diag_backoff_max_s: v0.38.0 I4 — per-iteration
        backoff ceiling.
    :param test_diag_backoff_total_budget_s: v0.38.0 I4 — cumulative
        backoff budget per task. Threading the cumulative-so-far via
        :meth:`test_diag_budget_exhausted` returns the hard-halt trip.
    :param test_diag_auto_reset_after_n_successes: v0.38.0 I4 —
        number of successful test runs within
        ``test_diag_auto_reset_window_s`` that clears the failure
        deque (and resets the backoff counter).
    :param test_diag_auto_reset_window_s: v0.38.0 I4 — rolling window
        for the auto-reset success counter.
    """

    def __init__(
        self,
        threshold: int = 3,
        window_s: float = 60.0,
        test_diag_threshold: int = 3,
        test_diag_window_s: float = 600.0,
        test_diag_diagnoses: frozenset[str] | None = None,
        test_diag_backoff_initial_s: float = 5.0,
        test_diag_backoff_multiplier: float = 2.0,
        test_diag_backoff_max_s: float = 120.0,
        test_diag_backoff_total_budget_s: float = 600.0,
        test_diag_auto_reset_after_n_successes: int = 3,
        test_diag_auto_reset_window_s: float = 900.0,
    ) -> None:
        self._threshold = threshold
        self._window_s = window_s
        # Stored as (task_id, subtype, timestamp) so a future
        # ``recent_failures()`` accessor can surface forensics without
        # changing the trip semantics. Eviction is lazy, in
        # ``should_halt`` — keeps ``record_failure`` O(1).
        self._failures: deque[tuple[str, str, datetime]] = deque()

        # v0.37.0 H3: independent test-diagnosis stream. Same
        # (task_id, label, ts) shape as ``_failures`` so the lazy-evict
        # pattern in ``should_halt`` works uniformly for both deques.
        self._test_diag_threshold = test_diag_threshold
        self._test_diag_window_s = test_diag_window_s
        self._test_diag_diagnoses = (
            test_diag_diagnoses
            if test_diag_diagnoses is not None
            else frozenset({"capture_failed"})
        )
        self._test_diag_failures: deque[tuple[str, str, datetime]] = deque()

        # v0.38.0 I4: backoff + auto-reset state for the test-diag
        # stream. The orchestrator drives backoff externally via
        # :meth:`next_backoff_s_for_test_diag` (returns the sleep, or
        # None when below threshold) and tracks ``cumulative_backoff_s``
        # per-task. The breaker raises the hard halt only via
        # :meth:`test_diag_budget_exhausted` once the cumulative
        # crosses the budget.
        self._test_diag_backoff_initial_s = test_diag_backoff_initial_s
        self._test_diag_backoff_multiplier = test_diag_backoff_multiplier
        self._test_diag_backoff_max_s = test_diag_backoff_max_s
        self._test_diag_backoff_total_budget_s = (
            test_diag_backoff_total_budget_s
        )
        # Number of times ``next_backoff_s_for_test_diag`` has returned
        # a non-None value since the last reset / auto-reset. Used as
        # the exponent ``n`` in ``initial * (multiplier ** (n - 1))``
        # so the first backoff is ``initial`` exactly.
        self._test_diag_failure_count_at_threshold: int = 0

        # Auto-reset success stream — independent deque, same lazy-
        # eviction shape as the failure deques. When the count reaches
        # ``after_n_successes`` within the window, the failure deque
        # and the backoff counter are both cleared.
        self._test_diag_auto_reset_after_n_successes = (
            test_diag_auto_reset_after_n_successes
        )
        self._test_diag_auto_reset_window_s = test_diag_auto_reset_window_s
        self._test_diag_successes: deque[tuple[str, datetime]] = deque()

    @property
    def threshold(self) -> int:
        return self._threshold

    @property
    def window_s(self) -> float:
        return self._window_s

    @property
    def test_diag_threshold(self) -> int:
        return self._test_diag_threshold

    @property
    def test_diag_window_s(self) -> float:
        return self._test_diag_window_s

    @property
    def test_diag_diagnoses(self) -> frozenset[str]:
        return self._test_diag_diagnoses

    @property
    def test_diag_backoff_initial_s(self) -> float:
        return self._test_diag_backoff_initial_s

    @property
    def test_diag_backoff_multiplier(self) -> float:
        return self._test_diag_backoff_multiplier

    @property
    def test_diag_backoff_max_s(self) -> float:
        return self._test_diag_backoff_max_s

    @property
    def test_diag_backoff_total_budget_s(self) -> float:
        return self._test_diag_backoff_total_budget_s

    @property
    def test_diag_auto_reset_after_n_successes(self) -> int:
        return self._test_diag_auto_reset_after_n_successes

    @property
    def test_diag_auto_reset_window_s(self) -> float:
        return self._test_diag_auto_reset_window_s

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

    def record_test_diagnosis(
        self, task_id: str, diagnosis: str, ts: datetime
    ) -> None:
        """v0.37.0 H3: record a test-runner infrastructure-class diagnosis.

        No-op if ``diagnosis`` is not in
        :attr:`test_diag_diagnoses` (mirrors the non-infra subtype
        skip in :meth:`record_failure`). Independent of the adapter-
        class counter — feeding this method does not affect the
        adapter-class deque, and vice versa.

        ``ts`` is taken explicitly so the orchestrator passes a single
        ``datetime.now(timezone.utc)`` per call site and tests can
        drive the breaker with a fixed clock.
        """
        if diagnosis not in self._test_diag_diagnoses:
            return
        # Normalize to UTC-aware so window math is consistent regardless
        # of whether the caller passed a naive UTC stamp or an aware one.
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        self._test_diag_failures.append((task_id, diagnosis, ts))

    def should_halt(self) -> tuple[bool, str | None]:
        """Return ``(True, reason)`` if the adapter-class stream's
        trip threshold is met.

        v0.38.0 I4: the test-diagnosis stream NO LONGER trips here on
        threshold-cross — the orchestrator drives the backoff loop
        externally via :meth:`next_backoff_s_for_test_diag` and the
        hard halt is gated on :meth:`test_diag_budget_exhausted` once
        the cumulative backoff crosses the budget. This method
        therefore reports only the adapter-class stream's status; the
        existing v0.30.0 contract (and log-grep anchors) for that
        stream is unchanged. The test-diag stream's reason text is
        synthesized inside :meth:`test_diag_budget_exhausted` instead.

        Lazy-evicts entries older than the adapter rolling window
        relative to the stream's most recent entry. Returns a reason
        string suitable for operator-facing messages (mentions the
        count and window explicitly). When the adapter stream is
        closed, returns ``(False, None)``.
        """
        # --- adapter-class stream (v0.30.0 — unchanged behaviour) ---
        if self._failures:
            # The "now" for window math is the most recent failure's
            # timestamp — NOT real-time. This means a long pause between
            # bursts doesn't artificially expire prior failures we already
            # judged safe; the next failure that arrives is what re-runs
            # the check and triggers eviction relative to its own timestamp.
            most_recent_ts = self._failures[-1][2]
            cutoff = most_recent_ts - timedelta(seconds=self._window_s)
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

        # v0.38.0 I4: test-diag stream no longer trips via this
        # method. The orchestrator handles backoff + budget externally.
        return (False, None)

    # -----------------------------------------------------------------
    # v0.38.0 I4 — test-diag backoff + auto-reset
    # -----------------------------------------------------------------

    def _evict_test_diag_failures(self) -> int:
        """Lazy-evict failure entries older than the test-diag window,
        returning the post-eviction count. Mirrors the inline pattern
        used in :meth:`should_halt` for the adapter stream.
        """
        if self._test_diag_failures:
            most_recent_ts = self._test_diag_failures[-1][2]
            cutoff = most_recent_ts - timedelta(
                seconds=self._test_diag_window_s
            )
            while (
                self._test_diag_failures
                and self._test_diag_failures[0][2] < cutoff
            ):
                self._test_diag_failures.popleft()
        return len(self._test_diag_failures)

    def _evict_test_diag_successes(self) -> int:
        """Lazy-evict success entries older than the auto-reset window."""
        if self._test_diag_successes:
            most_recent_ts = self._test_diag_successes[-1][1]
            cutoff = most_recent_ts - timedelta(
                seconds=self._test_diag_auto_reset_window_s
            )
            while (
                self._test_diag_successes
                and self._test_diag_successes[0][1] < cutoff
            ):
                self._test_diag_successes.popleft()
        return len(self._test_diag_successes)

    def record_test_success(self, task_id: str, ts: datetime) -> None:
        """v0.38.0 I4: record a successful test run for auto-reset.

        When the rolling-window success count reaches
        :attr:`test_diag_auto_reset_after_n_successes`, the failure
        deque AND the backoff counter are cleared — the runner is
        considered healthy again and a fresh flaky burst would need to
        cross the threshold from zero. Cheap, idempotent; safe to call
        defensively from the orchestrator's test-OK path.
        """
        # Normalize to UTC-aware so window math is consistent regardless
        # of whether the caller passed a naive UTC stamp or an aware one.
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        self._test_diag_successes.append((task_id, ts))
        count = self._evict_test_diag_successes()
        if count >= self._test_diag_auto_reset_after_n_successes:
            # Healthy run — clear the failure history and the backoff
            # exponent. Success deque stays in place so successive
            # successes don't re-trigger this branch needlessly; the
            # next failure burst is what re-arms via record_test_diagnosis.
            self._test_diag_failures.clear()
            self._test_diag_failure_count_at_threshold = 0
            try:
                # Best-effort structured log via the orchestrator's
                # logger — autologging is package-optional in some test
                # fixtures, so a bare except is correct here.
                from autologging import get_logger as _gl  # noqa: PLC0415

                _gl(__name__).info(
                    "circuit_breaker.test_diag_auto_reset",
                    successes=count,
                    window_s=self._test_diag_auto_reset_window_s,
                )
            except Exception:  # noqa: BLE001
                pass

    def next_backoff_s_for_test_diag(self) -> float | None:
        """v0.38.0 I4: compute the next backoff delay (seconds).

        Returns ``None`` when the current failure count is below the
        trip threshold — the orchestrator should NOT sleep and should
        let the existing per-task retry/hard-fail branch run. When
        threshold is crossed, increments the internal exponent and
        returns ``min(initial * (multiplier ** (n - 1)), max_s)``; the
        orchestrator sleeps for the returned value and accumulates it
        into a per-task ``cumulative_backoff_s``. Idempotent eviction
        of stale entries runs first so a long pause between bursts
        doesn't artificially keep the threshold met.
        """
        count = self._evict_test_diag_failures()
        if count < self._test_diag_threshold:
            return None
        # Crossed the threshold — increment the exponent counter so
        # ``n=1`` on the first crossing yields ``initial * multiplier ** 0
        # == initial``.
        self._test_diag_failure_count_at_threshold += 1
        n = self._test_diag_failure_count_at_threshold
        delay = self._test_diag_backoff_initial_s * (
            self._test_diag_backoff_multiplier ** (n - 1)
        )
        return min(delay, self._test_diag_backoff_max_s)

    def test_diag_budget_exhausted(
        self, cumulative_backoff_s: float
    ) -> tuple[bool, str | None]:
        """v0.38.0 I4: return ``(True, reason)`` when the per-task
        cumulative backoff has crossed the configured budget.

        The orchestrator threads its own ``cumulative_backoff_s``
        accumulator in (per-task local) and calls this after every
        :meth:`next_backoff_s_for_test_diag` to decide whether the
        next iteration should sleep-and-retry or raise the hard halt.
        Reason text mirrors the v0.37.0 H3 wording (mentions the
        test-diagnosis class, the dominant diagnosis label, and the
        budget) so operator log-greps stay aligned across versions.
        """
        if cumulative_backoff_s < self._test_diag_backoff_total_budget_s:
            return (False, None)
        # Pick the dominant diagnosis label from the most recent
        # failure for the operator message; deque is non-empty whenever
        # this is reached because the caller already accumulated
        # backoff via :meth:`next_backoff_s_for_test_diag`.
        dominant = (
            self._test_diag_failures[-1][1]
            if self._test_diag_failures
            else "capture_failed"
        )
        count = len(self._test_diag_failures)
        return (
            True,
            (
                f"test-diagnosis circuit open — {count} {dominant} exhausted "
                f"backoff budget {self._test_diag_backoff_total_budget_s:.0f}s. "
                f"Inspect runner; `autodev doctor`."
            ),
        )

    def reset(self) -> None:
        """Zero ALL counters (both streams + auto-reset state).
        Intentionally cheap and idempotent.

        v0.37.0 H3 distinction: the orchestrator's ``delegate()`` site
        only wants to clear the adapter-class stream on a successful
        adapter call (a healthy reviewer / developer return does NOT
        imply test-runner health — the whole point of the cross-task
        test-diag stream is to accumulate across many otherwise
        successful adapter calls). Production callers that want
        adapter-only reset use :meth:`reset_adapter` instead. Tests and
        explicit operator reset use this method.

        v0.38.0 I4: also clears the auto-reset success deque and the
        backoff exponent so a post-reset state is fully fresh.
        """
        self._failures.clear()
        self._test_diag_failures.clear()
        self._test_diag_successes.clear()
        self._test_diag_failure_count_at_threshold = 0

    def reset_adapter(self) -> None:
        """v0.37.0 H3: clear only the adapter-class deque.

        Called by the ``delegate()`` site on every adapter success.
        Preserves the test-diagnosis stream so the cross-task
        accumulation pattern is not erased by intervening healthy
        developer / reviewer calls.
        """
        self._failures.clear()


__all__ = ["InfraFailureCircuitBreaker", "INFRASTRUCTURE_SUBTYPES"]
