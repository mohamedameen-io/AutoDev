"""Unit tests for v0.37.0 H3 + v0.38.0 I4: test-diagnosis stream on
:class:`InfraFailureCircuitBreaker`.

A SECOND independent rolling counter on the existing breaker tracks
test-runner self-diagnoses (``capture_failed`` by default). The
adapter-class stream from v0.30.0 must remain semantically unchanged —
these tests cover the new stream plus the interleave with adapter-class
behaviour.

v0.38.0 I4 contract change: ``should_halt()`` no longer trips on the
test-diag stream alone. Threshold-cross now returns a backoff via
:meth:`next_backoff_s_for_test_diag`; the hard halt fires only when
:meth:`test_diag_budget_exhausted` returns trip. Tests that previously
called ``should_halt()`` on the test-diag stream have been migrated to
the new methods.
"""

from __future__ import annotations

import datetime as _dt


def _utc(seconds_offset: float = 0.0) -> _dt.datetime:
    """Return a fixed UTC base + offset so windowing is deterministic."""
    base = _dt.datetime(2026, 5, 13, 12, 0, 0, tzinfo=_dt.timezone.utc)
    return base + _dt.timedelta(seconds=seconds_offset)


def test_three_capture_failed_in_window_returns_backoff_not_halt() -> None:
    """v0.38.0 I4: the third ``capture_failed`` no longer hard-halts via
    ``should_halt()``. Instead :meth:`next_backoff_s_for_test_diag`
    returns the initial backoff (5.0s default); ``should_halt()``
    stays closed because the adapter-class stream is untouched.
    """
    from orchestrator.circuit_breaker import InfraFailureCircuitBreaker

    cb = InfraFailureCircuitBreaker()

    cb.record_test_diagnosis("t1", "capture_failed", _utc(0))
    halt, reason = cb.should_halt()
    assert halt is False
    assert reason is None
    # Below threshold — no backoff yet.
    assert cb.next_backoff_s_for_test_diag() is None

    cb.record_test_diagnosis("t2", "capture_failed", _utc(60))
    halt, _ = cb.should_halt()
    assert halt is False

    cb.record_test_diagnosis("t3", "capture_failed", _utc(120))
    # I4: should_halt() now stays closed on test-diag trip — backoff is
    # the new gate, hard halt waits on the budget.
    halt, reason = cb.should_halt()
    assert halt is False
    assert reason is None
    backoff = cb.next_backoff_s_for_test_diag()
    assert backoff == 5.0  # initial backoff


def test_two_in_window_plus_one_outside_does_not_trigger_backoff() -> None:
    """A capture_failed older than the rolling window must not count
    toward threshold, so no backoff is returned."""
    from orchestrator.circuit_breaker import InfraFailureCircuitBreaker

    cb = InfraFailureCircuitBreaker(
        test_diag_threshold=3, test_diag_window_s=600.0
    )

    # Two recent, one well outside the trailing window — the trailing
    # window is computed from the most recent failure (t=1300), so
    # t=0 is at -1300s and well outside the 600s window.
    cb.record_test_diagnosis("t1", "capture_failed", _utc(0))
    cb.record_test_diagnosis("t2", "capture_failed", _utc(1200))
    cb.record_test_diagnosis("t3", "capture_failed", _utc(1300))

    halt, reason = cb.should_halt()
    assert halt is False
    assert reason is None
    # Only 2 in window post-evict — below threshold.
    assert cb.next_backoff_s_for_test_diag() is None


def test_runtime_crash_ignored_by_default() -> None:
    """Default ``test_diag_diagnoses`` is ``{capture_failed}`` only."""
    from orchestrator.circuit_breaker import InfraFailureCircuitBreaker

    cb = InfraFailureCircuitBreaker()

    for i in range(5):
        cb.record_test_diagnosis(f"t{i}", "runtime_crash", _utc(i * 10))

    halt, _ = cb.should_halt()
    assert halt is False
    # No entries in the deque because ``runtime_crash`` is ignored by
    # default — no backoff either.
    assert cb.next_backoff_s_for_test_diag() is None


def test_runtime_crash_counts_when_in_configured_set() -> None:
    """Configuring the diagnoses set to include ``runtime_crash`` makes
    a mixed sequence cross the threshold (returns backoff under I4)."""
    from orchestrator.circuit_breaker import InfraFailureCircuitBreaker

    cb = InfraFailureCircuitBreaker(
        test_diag_threshold=3,
        test_diag_window_s=600.0,
        test_diag_diagnoses=frozenset(
            {"capture_failed", "runtime_crash"}
        ),
    )

    cb.record_test_diagnosis("t1", "capture_failed", _utc(0))
    cb.record_test_diagnosis("t2", "runtime_crash", _utc(60))
    assert cb.next_backoff_s_for_test_diag() is None

    cb.record_test_diagnosis("t3", "runtime_crash", _utc(120))
    # I4: threshold crossed — returns backoff, not halt.
    halt, reason = cb.should_halt()
    assert halt is False
    assert reason is None
    backoff = cb.next_backoff_s_for_test_diag()
    assert backoff == 5.0


def test_collection_failed_ignored_unless_configured() -> None:
    """``collection_failed`` is excluded from the default set; opt-in only."""
    from orchestrator.circuit_breaker import InfraFailureCircuitBreaker

    cb = InfraFailureCircuitBreaker()
    for i in range(4):
        cb.record_test_diagnosis(
            f"t{i}", "collection_failed", _utc(i * 10)
        )
    halt, _ = cb.should_halt()
    assert halt is False
    assert cb.next_backoff_s_for_test_diag() is None


def test_adapter_and_test_diag_streams_are_independent() -> None:
    """Interleaved adapter-class and test-diag failures count separately;
    adapter-class trip still flows through ``should_halt``, test-diag
    threshold-cross flows through ``next_backoff_s_for_test_diag``.
    """
    from orchestrator.circuit_breaker import InfraFailureCircuitBreaker

    cb = InfraFailureCircuitBreaker(
        threshold=3,
        window_s=60.0,
        test_diag_threshold=3,
        test_diag_window_s=600.0,
    )

    # One adapter-class failure, two test-diag failures — neither
    # threshold reached.
    cb.record_failure("t1", "auth_failed", _utc(0))
    cb.record_test_diagnosis("t2", "capture_failed", _utc(10))
    cb.record_test_diagnosis("t3", "capture_failed", _utc(20))
    halt, _ = cb.should_halt()
    assert halt is False
    assert cb.next_backoff_s_for_test_diag() is None

    # A third capture_failed crosses the test-diag threshold — returns
    # backoff under I4, but ``should_halt`` (adapter-only) stays closed.
    cb.record_test_diagnosis("t4", "capture_failed", _utc(30))
    halt, _ = cb.should_halt()
    assert halt is False
    backoff = cb.next_backoff_s_for_test_diag()
    assert backoff == 5.0


def test_adapter_stream_trip_still_halts_via_should_halt() -> None:
    """The adapter-class stream's hard-halt contract is unchanged in
    I4. Three ``auth_failed`` in window still trips ``should_halt``.
    """
    from orchestrator.circuit_breaker import InfraFailureCircuitBreaker

    cb = InfraFailureCircuitBreaker(
        threshold=3,
        window_s=60.0,
        test_diag_threshold=3,
        test_diag_window_s=600.0,
    )
    # Trip adapter stream:
    cb.record_failure("a1", "auth_failed", _utc(0))
    cb.record_failure("a2", "auth_failed", _utc(10))
    cb.record_failure("a3", "auth_failed", _utc(20))
    # Mix in a test-diag burst (above threshold) — must NOT change the
    # adapter-class trip behaviour.
    cb.record_test_diagnosis("t1", "capture_failed", _utc(30))
    cb.record_test_diagnosis("t2", "capture_failed", _utc(40))
    cb.record_test_diagnosis("t3", "capture_failed", _utc(50))

    halt, reason = cb.should_halt()
    assert halt is True
    assert reason is not None
    # v0.30.0 wording wins so existing operator log-greps still match.
    assert "infrastructure circuit open" in reason


def test_reset_clears_both_deques_and_backoff_state() -> None:
    """``reset()`` is the explicit operator-facing zero; both deques
    and the I4 backoff-exponent counter must be cleared so a post-
    reset trip needs threshold-fresh events."""
    from orchestrator.circuit_breaker import InfraFailureCircuitBreaker

    cb = InfraFailureCircuitBreaker()

    cb.record_failure("a1", "auth_failed", _utc(0))
    cb.record_failure("a2", "auth_failed", _utc(5))
    cb.record_test_diagnosis("t1", "capture_failed", _utc(10))
    cb.record_test_diagnosis("t2", "capture_failed", _utc(20))
    cb.record_test_diagnosis("t3", "capture_failed", _utc(30))
    # Crossed threshold once — exponent now 1.
    cb.next_backoff_s_for_test_diag()

    cb.reset()

    # After reset, two more on each stream must NOT trip — need full
    # threshold worth of fresh events.
    cb.record_failure("a3", "auth_failed", _utc(40))
    cb.record_test_diagnosis("t4", "capture_failed", _utc(50))
    halt, _ = cb.should_halt()
    assert halt is False
    assert cb.next_backoff_s_for_test_diag() is None


def test_reset_adapter_only_clears_adapter_stream() -> None:
    """``reset_adapter()`` is the delegate-site path: clears adapter-
    class only so the test-diag accumulation persists across healthy
    intervening adapter calls (the whole point of cross-task)."""
    from orchestrator.circuit_breaker import InfraFailureCircuitBreaker

    cb = InfraFailureCircuitBreaker()

    cb.record_failure("a1", "auth_failed", _utc(0))
    cb.record_test_diagnosis("t1", "capture_failed", _utc(10))
    cb.record_test_diagnosis("t2", "capture_failed", _utc(20))

    cb.reset_adapter()

    # Test-diag stream still has 2 — a third capture_failed crosses
    # the threshold and returns backoff (I4 contract).
    cb.record_test_diagnosis("t3", "capture_failed", _utc(30))
    halt, _ = cb.should_halt()
    assert halt is False
    backoff = cb.next_backoff_s_for_test_diag()
    assert backoff == 5.0


def test_test_diag_diagnoses_property_default_is_capture_failed() -> None:
    """Sanity: the default-constructed breaker exposes
    ``capture_failed`` only via the public property."""
    from orchestrator.circuit_breaker import InfraFailureCircuitBreaker

    cb = InfraFailureCircuitBreaker()
    assert cb.test_diag_diagnoses == frozenset({"capture_failed"})
    assert cb.test_diag_threshold == 3
    assert cb.test_diag_window_s == 600.0
    # v0.38.0 I4 defaults exposed via properties.
    assert cb.test_diag_backoff_initial_s == 5.0
    assert cb.test_diag_backoff_multiplier == 2.0
    assert cb.test_diag_backoff_max_s == 120.0
    assert cb.test_diag_backoff_total_budget_s == 600.0
    assert cb.test_diag_auto_reset_after_n_successes == 3
    assert cb.test_diag_auto_reset_window_s == 900.0
