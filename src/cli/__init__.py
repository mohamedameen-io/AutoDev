"""CLI entry point."""

from __future__ import annotations

import logging
import signal
import sys

import click

from _version import __version__
from cli.commands import register_commands


_log = logging.getLogger(__name__)


def _install_signal_handlers() -> None:
    """v0.23.0 C3: install SIGTERM/SIGHUP handlers.

    Click's ``standalone_mode=True`` already translates SIGINT into a
    clean ``Abort`` (KeyboardInterrupt → click.exceptions.Abort →
    sys.exit(1)). For SIGTERM (default kill signal) and SIGHUP (terminal
    disconnect), no handler is installed by default — the process dies
    silently with no breadcrumb. We install a handler that logs the
    signal name and re-raises ``SystemExit`` so any ``finally`` blocks
    (notably :func:`plan_lock`'s release) still run.

    Best-effort: ``signal.signal`` only works on the main thread; if
    we're called from a worker thread (rare for CLI entry) the install
    is silently skipped.
    """
    def _handler(signum: int, _frame: object) -> None:  # noqa: ARG001
        sig_name = signal.Signals(signum).name
        _log.warning("cli.shutdown_signal received signal=%s", sig_name)
        # SystemExit propagates through ``finally`` blocks (unlike os._exit),
        # giving plan_lock and other context managers a chance to release.
        raise SystemExit(128 + signum)

    try:
        signal.signal(signal.SIGTERM, _handler)
    except (ValueError, OSError):
        # Not on main thread, or platform without SIGTERM (Windows).
        pass
    if hasattr(signal, "SIGHUP"):
        try:
            signal.signal(signal.SIGHUP, _handler)  # type: ignore[attr-defined]
        except (ValueError, OSError):
            pass


@click.group(
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=False,
)
@click.version_option(version=__version__, prog_name="autodev")
def cli() -> None:
    """autodev: multi-agent orchestrator with tournament self-refinement."""


register_commands(cli)


def main() -> None:
    """Console-script entry point."""
    _install_signal_handlers()
    cli(standalone_mode=True)
