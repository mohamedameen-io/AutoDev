"""v0.24.0 D2 regression: ``run_sandboxed`` enforces hard wall-clock timeouts.

The asyncio watchdog (v0.22.1 A1) is cooperative — it cannot kill a
CPU-bound thread mid-flight. The sandbox runs the function in a
separate process so the OS can SIGKILL it on timeout.
"""

from __future__ import annotations

import time

import pytest

from qa.sandbox import run_sandboxed


# Module-level functions so the multiprocessing pickle round-trip works.


def _fast_returner(x: int) -> int:
    return x * 2


def _slow_function() -> int:
    time.sleep(5)
    return 99


def _raises_value_error() -> None:
    raise ValueError("boom")


def test_run_sandboxed_returns_result_when_within_budget() -> None:
    assert run_sandboxed(_fast_returner, 21, timeout_s=5.0) == 42


def test_run_sandboxed_propagates_exception_from_worker() -> None:
    with pytest.raises(ValueError, match="boom"):
        run_sandboxed(_raises_value_error, timeout_s=5.0)


def test_run_sandboxed_kills_runaway_worker() -> None:
    """A worker that blows past the timeout is killed, callback is invoked."""
    start = time.time()
    result = run_sandboxed(
        _slow_function,
        timeout_s=0.5,
        on_timeout=lambda: -1,
    )
    elapsed = time.time() - start
    assert result == -1
    # We should NOT have waited the full 5 seconds.
    assert elapsed < 4.0, f"sandbox took {elapsed:.2f}s — should be ~0.5s"


def test_run_sandboxed_no_callback_raises_timeout() -> None:
    """Without on_timeout, the underlying TimeoutError propagates."""
    import concurrent.futures

    with pytest.raises(concurrent.futures.TimeoutError):
        run_sandboxed(_slow_function, timeout_s=0.5)
