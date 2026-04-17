"""Tests for :mod:`autologging` -- configure, get_logger, file_sink_path."""

from __future__ import annotations

from pathlib import Path


from autologging import configure, file_sink_path, get_logger


# ---------------------------------------------------------------------------
# configure()
# ---------------------------------------------------------------------------


def test_configure_json_output() -> None:
    """configure() with default json_output=True completes without error."""
    configure(json_output=True)


def test_configure_console_output() -> None:
    """configure(json_output=False) selects ConsoleRenderer without error."""
    configure(json_output=False)


# ---------------------------------------------------------------------------
# get_logger()
# ---------------------------------------------------------------------------


def test_get_logger_returns_bound_logger() -> None:
    """get_logger() returns a structlog BoundLogger (or proxy)."""
    configure()
    log = get_logger()
    # structlog loggers expose standard level methods
    assert hasattr(log, "info")
    assert hasattr(log, "warning")
    assert hasattr(log, "error")


def test_get_logger_with_session_id() -> None:
    """session_id is bound when provided."""
    configure()
    log = get_logger(session_id="sess-abc-123")
    # The bound logger should carry session_id in its bindings.
    # structlog's FilteringBoundLogger stores bindings in _context.
    ctx = getattr(log, "_context", {})
    assert ctx.get("session_id") == "sess-abc-123"


def test_get_logger_with_bindings() -> None:
    """Extra keyword arguments are bound to the logger."""
    configure()
    log = get_logger(component="qa", run_id=42)
    ctx = getattr(log, "_context", {})
    assert ctx.get("component") == "qa"
    assert ctx.get("run_id") == 42


# ---------------------------------------------------------------------------
# file_sink_path()
# ---------------------------------------------------------------------------


def test_file_sink_path() -> None:
    """file_sink_path returns the canonical events.jsonl path."""
    root = Path("/projects/my-app")
    result = file_sink_path("sess-001", root)
    expected = root / ".autodev" / "sessions" / "sess-001" / "events.jsonl"
    assert result == expected
