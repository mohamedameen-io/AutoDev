"""Unit tests for v0.37.0 H3: test-diagnosis stream on
:class:`InfraFailureCircuitBreaker`.

A SECOND independent rolling counter on the existing breaker tracks
test-runner self-diagnoses (``capture_failed`` by default). The
adapter-class stream from v0.30.0 must remain semantically unchanged —
these tests cover the new stream plus the interleave with adapter-class
behaviour.
"""

from __future__ import annotations

import datetime as _dt


def _utc(seconds_offset: float = 0.0) -> _dt.datetime:
    """Return a fixed UTC base + offset so windowing is deterministic."""
    base = _dt.datetime(2026, 5, 13, 12, 0, 0, tzinfo=_dt.timezone.utc)
    return base + _dt.timedelta(seconds=seconds_offset)


def test_three_capture_failed_in_window_opens_test_diag_stream() -> None:
    """Default config (3 in 600s): the third ``capture_failed`` trips."""
    from orchestrator.circuit_breaker import InfraFailureCircuitBreaker

    cb = InfraFailureCircuitBreaker()

    cb.record_test_diagnosis("t1", "capture_failed", _utc(0))
    halt, reason = cb.should_halt()
    assert halt is False
    assert reason is None

    cb.record_test_diagnosis("t2", "capture_failed", _utc(60))
    halt, _ = cb.should_halt()
    assert halt is False

    cb.record_test_diagnosis("t3", "capture_failed", _utc(120))
    halt, reason = cb.should_halt()
    assert halt is True
    assert reason is not None
    # Operator-facing — must mention the count, window, and the test-
    # diagnosis label so logs/console make the trip class obvious.
    assert "test-diagnosis" in reason
    assert "3" in reason
    assert "capture_failed" in reason
    assert "600" in reason


def test_two_in_window_plus_one_outside_does_not_trip() -> None:
    """A capture_failed older than the rolling window must not count."""
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


def test_runtime_crash_ignored_by_default() -> None:
    """Default ``test_diag_diagnoses`` is ``{capture_failed}`` only."""
    from orchestrator.circuit_breaker import InfraFailureCircuitBreaker

    cb = InfraFailureCircuitBreaker()

    for i in range(5):
        cb.record_test_diagnosis(f"t{i}", "runtime_crash", _utc(i * 10))

    halt, _ = cb.should_halt()
    assert halt is False


def test_runtime_crash_counts_when_in_configured_set() -> None:
    """Configuring the diagnoses set to include ``runtime_crash`` makes
    a mixed sequence trip."""
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
    halt, _ = cb.should_halt()
    assert halt is False

    cb.record_test_diagnosis("t3", "runtime_crash", _utc(120))
    halt, reason = cb.should_halt()
    assert halt is True
    assert reason is not None
    assert "test-diagnosis" in reason


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


def test_adapter_and_test_diag_streams_are_independent() -> None:
    """Interleaved adapter-class and test-diag failures count separately;
    each trips its own threshold without the other reaching it.
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

    # A third capture_failed trips the test-diag stream only — the
    # adapter-class stream still has just one entry.
    cb.record_test_diagnosis("t4", "capture_failed", _utc(30))
    halt, reason = cb.should_halt()
    assert halt is True
    assert reason is not None
    assert "test-diagnosis" in reason
    assert "capture_failed" in reason


def test_adapter_stream_trip_preferred_when_both_would_trip() -> None:
    """When both streams would trip, the adapter-class reason is
    returned first (preserves the v0.30.0 message + log anchors)."""
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
    # Trip test-diag stream:
    cb.record_test_diagnosis("t1", "capture_failed", _utc(30))
    cb.record_test_diagnosis("t2", "capture_failed", _utc(40))
    cb.record_test_diagnosis("t3", "capture_failed", _utc(50))

    halt, reason = cb.should_halt()
    assert halt is True
    assert reason is not None
    # v0.30.0 wording wins so existing operator log-greps still match.
    assert "infrastructure circuit open" in reason
    assert "test-diagnosis" not in reason


def test_reset_clears_both_deques() -> None:
    """``reset()`` is the explicit operator-facing zero; both deques
    must be cleared so a post-reset trip needs threshold-fresh events."""
    from orchestrator.circuit_breaker import InfraFailureCircuitBreaker

    cb = InfraFailureCircuitBreaker()

    cb.record_failure("a1", "auth_failed", _utc(0))
    cb.record_failure("a2", "auth_failed", _utc(5))
    cb.record_test_diagnosis("t1", "capture_failed", _utc(10))
    cb.record_test_diagnosis("t2", "capture_failed", _utc(20))

    cb.reset()

    # After reset, two more on each stream must NOT trip — need full
    # threshold worth of fresh events.
    cb.record_failure("a3", "auth_failed", _utc(30))
    cb.record_test_diagnosis("t3", "capture_failed", _utc(40))
    halt, _ = cb.should_halt()
    assert halt is False


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

    # Test-diag stream still has 2 — a third capture_failed trips.
    cb.record_test_diagnosis("t3", "capture_failed", _utc(30))
    halt, reason = cb.should_halt()
    assert halt is True
    assert reason is not None
    assert "test-diagnosis" in reason


def test_test_diag_diagnoses_property_default_is_capture_failed() -> None:
    """Sanity: the default-constructed breaker exposes
    ``capture_failed`` only via the public property."""
    from orchestrator.circuit_breaker import InfraFailureCircuitBreaker

    cb = InfraFailureCircuitBreaker()
    assert cb.test_diag_diagnoses == frozenset({"capture_failed"})
    assert cb.test_diag_threshold == 3
    assert cb.test_diag_window_s == 600.0
