"""Tests for :mod:`autologging` -- configure, get_logger, file_sink_path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autologging import (
    attach_session_file_sink,
    configure,
    detach_session_file_sink,
    file_sink_path,
    get_logger,
)


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


# ---------------------------------------------------------------------------
# attach_session_file_sink / detach_session_file_sink (v0.25.2)
# ---------------------------------------------------------------------------


@pytest.fixture
def _fresh_structlog() -> None:
    """Ensure structlog has the v0.25.2 processor chain installed."""
    configure(level="DEBUG")


def test_attach_session_file_sink_creates_parent_dirs(
    tmp_path: Path, _fresh_structlog: None
) -> None:
    """First attach materializes ``.autodev/sessions/<sid>/events.jsonl``
    and its parent directories."""
    sid = "sess-test-creates-dirs"
    path = tmp_path / ".autodev" / "sessions" / sid / "events.jsonl"
    assert not path.parent.exists()

    attach_session_file_sink(sid, tmp_path)
    try:
        assert path.parent.exists()
        assert path.exists()
    finally:
        detach_session_file_sink(sid)


def test_attach_session_file_sink_appends_emitted_lines(
    tmp_path: Path, _fresh_structlog: None
) -> None:
    """Logs emitted by ``get_logger(session_id=sid)`` after attach are
    written as JSON lines to the sink file."""
    sid = "sess-test-appends"
    attach_session_file_sink(sid, tmp_path)
    try:
        log = get_logger(session_id=sid)
        log.info("test.event_a", key="value_a")
        log.info("test.event_b", key="value_b")
    finally:
        detach_session_file_sink(sid)

    path = tmp_path / ".autodev" / "sessions" / sid / "events.jsonl"
    assert path.exists()
    lines = [
        json.loads(ln)
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln
    ]
    events = [ln["event"] for ln in lines]
    assert "test.event_a" in events
    assert "test.event_b" in events
    for ln in lines:
        assert ln["session_id"] == sid


def test_attach_session_file_sink_is_idempotent(
    tmp_path: Path, _fresh_structlog: None
) -> None:
    """Calling attach twice for the same session does not truncate
    existing content or duplicate the handle."""
    sid = "sess-test-idempotent"
    attach_session_file_sink(sid, tmp_path)
    try:
        get_logger(session_id=sid).info("test.first")
        attach_session_file_sink(sid, tmp_path)  # second call: no-op
        get_logger(session_id=sid).info("test.second")
    finally:
        detach_session_file_sink(sid)

    path = tmp_path / ".autodev" / "sessions" / sid / "events.jsonl"
    events = [
        json.loads(ln)["event"]
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln
    ]
    assert "test.first" in events
    assert "test.second" in events


def test_unmatched_session_id_writes_nothing(
    tmp_path: Path, _fresh_structlog: None
) -> None:
    """A log bound to a session_id with no registered sink does NOT
    create stray files or leak into other sinks."""
    sid_attached = "sess-attached"
    sid_other = "sess-other"
    attach_session_file_sink(sid_attached, tmp_path)
    try:
        get_logger(session_id=sid_other).info("test.unmatched")
    finally:
        detach_session_file_sink(sid_attached)

    path = tmp_path / ".autodev" / "sessions" / sid_attached / "events.jsonl"
    content = path.read_text(encoding="utf-8")
    assert "test.unmatched" not in content
    assert not (tmp_path / ".autodev" / "sessions" / sid_other).exists()
