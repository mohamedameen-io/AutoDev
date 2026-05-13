"""Unit tests for v0.30.0 Bug 5: :class:`InfraFailureCircuitBreaker`.

The breaker counts adapter failures whose ``subtype`` falls into the
infrastructure class (``auth_failed``, ``rate_limited``, ``server_error``)
and trips when the rolling-window count crosses a threshold. Successful
adapter results reset the counter; deterministic per-task subtypes
(``client_error``, ``error_max_turns``, ``None``) are ignored.

Co-located with :mod:`tests.test_orchestrator_circuit_breaker_integration`
which exercises the orchestrator wiring.
"""

from __future__ import annotations

import datetime as _dt

import pytest


def _utc(seconds_offset: float = 0.0) -> _dt.datetime:
    """Return a fixed UTC base + offset so windowing is deterministic."""
    base = _dt.datetime(2026, 5, 13, 12, 0, 0, tzinfo=_dt.timezone.utc)
    return base + _dt.timedelta(seconds=seconds_offset)


def test_three_auth_failures_in_60s_opens_breaker() -> None:
    """Threshold-1 failures stay closed; the threshold-th opens."""
    from orchestrator.circuit_breaker import InfraFailureCircuitBreaker

    cb = InfraFailureCircuitBreaker(threshold=3, window_s=60.0)

    cb.record_failure("t1", "auth_failed", _utc(0))
    halt, reason = cb.should_halt()
    assert halt is False
    assert reason is None

    cb.record_failure("t2", "auth_failed", _utc(10))
    halt, reason = cb.should_halt()
    assert halt is False

    cb.record_failure("t3", "auth_failed", _utc(20))
    halt, reason = cb.should_halt()
    assert halt is True
    assert reason is not None
    # Reason text is operator-facing — must mention the count and window
    # so the message that the orchestrator surfaces stays actionable.
    assert "3" in reason
    assert "60" in reason


def test_failures_outside_window_dont_count() -> None:
    """Two failures >60s before the third must NOT contribute."""
    from orchestrator.circuit_breaker import InfraFailureCircuitBreaker

    cb = InfraFailureCircuitBreaker(threshold=3, window_s=60.0)

    # Three failures, but the first two fall outside the trailing window
    # of the most recent failure.
    cb.record_failure("t1", "server_error", _utc(0))
    cb.record_failure("t2", "server_error", _utc(10))
    halt, _ = cb.should_halt()
    assert halt is False

    # Two minutes later — the first two have aged out.
    cb.record_failure("t3", "server_error", _utc(120))
    halt, reason = cb.should_halt()
    assert halt is False
    assert reason is None


def test_successful_call_resets_breaker() -> None:
    """``reset()`` zeroes the counter — a subsequent threshold-1 stays closed."""
    from orchestrator.circuit_breaker import InfraFailureCircuitBreaker

    cb = InfraFailureCircuitBreaker(threshold=3, window_s=60.0)

    cb.record_failure("t1", "auth_failed", _utc(0))
    cb.record_failure("t2", "auth_failed", _utc(5))
    cb.reset()

    # After reset, two more failures must NOT trip — would need 3 fresh
    # ones inside the window.
    cb.record_failure("t3", "auth_failed", _utc(10))
    cb.record_failure("t4", "auth_failed", _utc(20))
    halt, _ = cb.should_halt()
    assert halt is False


def test_non_infrastructure_subtypes_dont_count() -> None:
    """``client_error``, ``error_max_turns``, ``None`` are per-task verdicts."""
    from orchestrator.circuit_breaker import InfraFailureCircuitBreaker

    cb = InfraFailureCircuitBreaker(threshold=3, window_s=60.0)

    # All non-infra — three of each, none should trip the breaker.
    for i, subtype in enumerate(
        ["client_error", "error_max_turns", None, "unknown_subtype"]
    ):
        cb.record_failure(f"t{i}", subtype, _utc(i))

    halt, reason = cb.should_halt()
    assert halt is False
    assert reason is None
