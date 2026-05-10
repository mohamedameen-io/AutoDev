"""v0.23.0 C3 regression: CLI installs SIGTERM/SIGHUP handlers.

D-6 finding from the 2026-05-09 Unity stall: SIGTERM exited the
orchestrator silently with no log breadcrumb. Now the CLI installs a
handler that logs the signal name and re-raises ``SystemExit`` so
``finally`` blocks (notably :func:`plan_lock` release) still run.
"""

from __future__ import annotations

import logging
import signal

import pytest

from cli import _install_signal_handlers


def test_install_signal_handlers_idempotent_main_thread() -> None:
    """Calling install repeatedly on the main thread succeeds."""
    _install_signal_handlers()
    _install_signal_handlers()  # idempotent
    # SIGTERM handler is installed (default would be SIG_DFL).
    current = signal.getsignal(signal.SIGTERM)
    assert current is not signal.SIG_DFL
    assert callable(current) or current == signal.SIG_IGN


def test_install_signal_handlers_no_crash_in_thread() -> None:
    """Install on a worker thread no-ops without raising."""
    import threading

    err: list[BaseException] = []

    def _runner() -> None:
        try:
            _install_signal_handlers()
        except BaseException as exc:  # noqa: BLE001
            err.append(exc)

    t = threading.Thread(target=_runner)
    t.start()
    t.join()
    assert err == []


def test_signal_handler_raises_systemexit(caplog: pytest.LogCaptureFixture) -> None:
    """The handler raises SystemExit(128 + signum) and logs the signal name."""
    _install_signal_handlers()
    handler = signal.getsignal(signal.SIGTERM)
    assert callable(handler)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(SystemExit) as excinfo:
            handler(signal.SIGTERM, None)  # type: ignore[arg-type]
    assert excinfo.value.code == 128 + signal.SIGTERM
    assert any("cli.shutdown_signal" in r.message for r in caplog.records)
