"""Structlog JSON-line configuration."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import structlog


def configure(level: str = "INFO", json_output: bool = True) -> None:
    """Configure stdlib logging and structlog with a shared JSON processor chain."""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if json_output:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(session_id: str | None = None, **bindings: Any) -> structlog.BoundLogger:
    """Return a bound structlog logger; binds session_id when present."""
    log = structlog.get_logger()
    if session_id is not None:
        log = log.bind(session_id=session_id)
    if bindings:
        log = log.bind(**bindings)
    return log


# TODO(phase-4): file sink to .autodev/sessions/{session_id}/events.jsonl once
# session lifecycle exists. Phase 1 keeps stdout-only logging.
def file_sink_path(session_id: str, project_root: Path) -> Path:
    """Return the intended events.jsonl path for a session (not yet written)."""
    return project_root / ".autodev" / "sessions" / session_id / "events.jsonl"
