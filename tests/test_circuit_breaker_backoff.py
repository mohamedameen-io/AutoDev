"""v0.38.0 I4: exponential backoff + auto-reset for the test-diag
stream on :class:`InfraFailureCircuitBreaker`.

Pins the new contract:

* ``next_backoff_s_for_test_diag`` returns ``None`` below threshold
  and an exponentially-growing sleep capped at ``max_s`` above it.
* ``test_diag_budget_exhausted(cumulative_s)`` returns trip when the
  cumulative crosses the configured budget.
* ``record_test_success`` × N within the auto-reset window clears the
  failure deque and the backoff exponent so a healthy run recovers.
* ``reset()`` clears the new I4 state (successes deque + exponent
  counter) in addition to the existing failure deques.
"""

from __future__ import annotations

import datetime as _dt


def _utc(seconds_offset: float = 0.0) -> _dt.datetime:
    base = _dt.datetime(2026, 5, 13, 12, 0, 0, tzinfo=_dt.timezone.utc)
    return base + _dt.timedelta(seconds=seconds_offset)


# ---------------------------------------------------------------------------
# Backoff growth
# ---------------------------------------------------------------------------


def test_three_capture_failed_returns_initial_backoff() -> None:
    """First threshold cross returns ``initial`` exactly (default 5.0)."""
    from orchestrator.circuit_breaker import InfraFailureCircuitBreaker

    cb = InfraFailureCircuitBreaker()
    for i in range(3):
        cb.record_test_diagnosis(f"t{i}", "capture_failed", _utc(i * 10))
    assert cb.next_backoff_s_for_test_diag() == 5.0


def test_fourth_capture_failed_returns_doubled_backoff() -> None:
    """Second threshold cross returns ``initial * multiplier`` (10.0)."""
    from orchestrator.circuit_breaker import InfraFailureCircuitBreaker

    cb = InfraFailureCircuitBreaker()
    for i in range(3):
        cb.record_test_diagnosis(f"t{i}", "capture_failed", _utc(i * 10))
    # First crossing -> 5.0
    cb.next_backoff_s_for_test_diag()
    # Fourth event -> second crossing -> 10.0
    cb.record_test_diagnosis("t4", "capture_failed", _utc(40))
    assert cb.next_backoff_s_for_test_diag() == 10.0


def test_fifth_capture_failed_returns_4x_backoff() -> None:
    """Third threshold cross returns ``initial * multiplier ** 2`` (20.0)."""
    from orchestrator.circuit_breaker import InfraFailureCircuitBreaker

    cb = InfraFailureCircuitBreaker()
    for i in range(3):
        cb.record_test_diagnosis(f"t{i}", "capture_failed", _utc(i * 10))
    cb.next_backoff_s_for_test_diag()  # 5.0
    cb.record_test_diagnosis("t4", "capture_failed", _utc(40))
    cb.next_backoff_s_for_test_diag()  # 10.0
    cb.record_test_diagnosis("t5", "capture_failed", _utc(50))
    assert cb.next_backoff_s_for_test_diag() == 20.0


def test_backoff_caps_at_max_s() -> None:
    """Growth is bounded by ``test_diag_backoff_max_s`` (default 120.0)."""
    from orchestrator.circuit_breaker import InfraFailureCircuitBreaker

    cb = InfraFailureCircuitBreaker()
    for i in range(3):
        cb.record_test_diagnosis(f"t{i}", "capture_failed", _utc(i * 10))
    # Crank the exponent up; values would be 5, 10, 20, 40, 80, 160 → capped 120.
    seen: list[float | None] = []
    for j in range(6):
        if j > 0:
            cb.record_test_diagnosis(
                f"t_extra_{j}", "capture_failed", _utc(60 + j * 10)
            )
        seen.append(cb.next_backoff_s_for_test_diag())
    assert seen == [5.0, 10.0, 20.0, 40.0, 80.0, 120.0]
    # One more — still capped.
    cb.record_test_diagnosis("t_more", "capture_failed", _utc(200))
    assert cb.next_backoff_s_for_test_diag() == 120.0


def test_next_backoff_below_threshold_returns_none() -> None:
    """No backoff fires until the threshold is crossed."""
    from orchestrator.circuit_breaker import InfraFailureCircuitBreaker

    cb = InfraFailureCircuitBreaker(test_diag_threshold=3)
    cb.record_test_diagnosis("t1", "capture_failed", _utc(0))
    cb.record_test_diagnosis("t2", "capture_failed", _utc(10))
    assert cb.next_backoff_s_for_test_diag() is None


# ---------------------------------------------------------------------------
# Budget exhaustion
# ---------------------------------------------------------------------------


def test_budget_exhausted_at_default_budget() -> None:
    """``test_diag_budget_exhausted(600.0)`` trips at the default budget."""
    from orchestrator.circuit_breaker import InfraFailureCircuitBreaker

    cb = InfraFailureCircuitBreaker()
    # Need at least one failure on the deque so the dominant-label
    # message synthesis path runs.
    cb.record_test_diagnosis("t1", "capture_failed", _utc(0))

    tripped, reason = cb.test_diag_budget_exhausted(cumulative_backoff_s=600.0)
    assert tripped is True
    assert reason is not None
    assert "test-diagnosis" in reason
    assert "capture_failed" in reason
    assert "600" in reason


def test_budget_not_exhausted_below_ceiling() -> None:
    """``test_diag_budget_exhausted`` returns ``(False, None)`` below."""
    from orchestrator.circuit_breaker import InfraFailureCircuitBreaker

    cb = InfraFailureCircuitBreaker()
    tripped, reason = cb.test_diag_budget_exhausted(cumulative_backoff_s=599.9)
    assert tripped is False
    assert reason is None


def test_custom_budget_threshold() -> None:
    """Custom budget value gates the trip."""
    from orchestrator.circuit_breaker import InfraFailureCircuitBreaker

    cb = InfraFailureCircuitBreaker(test_diag_backoff_total_budget_s=20.0)
    cb.record_test_diagnosis("t1", "capture_failed", _utc(0))

    tripped, _ = cb.test_diag_budget_exhausted(cumulative_backoff_s=19.9)
    assert tripped is False
    tripped, _ = cb.test_diag_budget_exhausted(cumulative_backoff_s=20.0)
    assert tripped is True


# ---------------------------------------------------------------------------
# Auto-reset via record_test_success
# ---------------------------------------------------------------------------


def test_three_successes_in_window_clears_failure_deque() -> None:
    """3 successes within ``auto_reset_window_s`` clears failure deque
    AND the backoff exponent so a fresh burst starts from scratch."""
    from orchestrator.circuit_breaker import InfraFailureCircuitBreaker

    cb = InfraFailureCircuitBreaker()
    # Cross threshold once.
    for i in range(3):
        cb.record_test_diagnosis(f"t{i}", "capture_failed", _utc(i * 10))
    assert cb.next_backoff_s_for_test_diag() == 5.0

    # 3 successes in the auto-reset window → clears.
    for j in range(3):
        cb.record_test_success(f"ok_{j}", _utc(60 + j * 10))

    # Deque cleared → below threshold → no backoff.
    assert cb.next_backoff_s_for_test_diag() is None
    # New burst needs full threshold of fresh events.
    cb.record_test_diagnosis("t_new", "capture_failed", _utc(200))
    assert cb.next_backoff_s_for_test_diag() is None  # still 1/3
    cb.record_test_diagnosis("t_new2", "capture_failed", _utc(210))
    assert cb.next_backoff_s_for_test_diag() is None  # still 2/3
    cb.record_test_diagnosis("t_new3", "capture_failed", _utc(220))
    # And exponent restarted: first backoff back to initial (5.0).
    assert cb.next_backoff_s_for_test_diag() == 5.0


def test_two_successes_does_not_clear() -> None:
    """Below ``after_n_successes`` — failure deque persists."""
    from orchestrator.circuit_breaker import InfraFailureCircuitBreaker

    cb = InfraFailureCircuitBreaker()
    for i in range(3):
        cb.record_test_diagnosis(f"t{i}", "capture_failed", _utc(i * 10))
    cb.record_test_success("ok_1", _utc(60))
    cb.record_test_success("ok_2", _utc(70))

    # Still 3 capture_failed in deque → threshold met → backoff returns.
    assert cb.next_backoff_s_for_test_diag() == 5.0


def test_three_successes_outside_window_does_not_clear() -> None:
    """Successes older than ``auto_reset_window_s`` evict before the
    counter ever reaches the threshold."""
    from orchestrator.circuit_breaker import InfraFailureCircuitBreaker

    # Tight auto-reset window so we can drive eviction with fixed
    # timestamps. Default 900s is wider than we want here.
    cb = InfraFailureCircuitBreaker(
        test_diag_auto_reset_window_s=100.0,
    )
    for i in range(3):
        cb.record_test_diagnosis(f"t{i}", "capture_failed", _utc(i * 10))

    # 3 successes spread across > window — each successive call evicts
    # the older entries before the count crosses the threshold.
    cb.record_test_success("ok_1", _utc(100))
    cb.record_test_success("ok_2", _utc(300))  # evicts ok_1
    cb.record_test_success("ok_3", _utc(500))  # evicts ok_2

    # Deque still has 3 capture_failed → threshold met → backoff returns.
    assert cb.next_backoff_s_for_test_diag() == 5.0


# ---------------------------------------------------------------------------
# Reset semantics
# ---------------------------------------------------------------------------


def test_reset_clears_successes_and_exponent() -> None:
    """``reset()`` also clears the I4 successes deque + backoff exponent."""
    from orchestrator.circuit_breaker import InfraFailureCircuitBreaker

    cb = InfraFailureCircuitBreaker()
    for i in range(3):
        cb.record_test_diagnosis(f"t{i}", "capture_failed", _utc(i * 10))
    cb.next_backoff_s_for_test_diag()  # bump exponent to 1
    cb.record_test_diagnosis("t_extra", "capture_failed", _utc(40))
    cb.next_backoff_s_for_test_diag()  # bump exponent to 2
    cb.record_test_success("ok_1", _utc(60))

    cb.reset()

    # Re-trip — must start over at the initial backoff (5.0).
    for i in range(3):
        cb.record_test_diagnosis(
            f"t_post_{i}", "capture_failed", _utc(200 + i * 10)
        )
    assert cb.next_backoff_s_for_test_diag() == 5.0
