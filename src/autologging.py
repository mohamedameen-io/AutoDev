"""Structlog JSON-line configuration + per-session file sink."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog


if TYPE_CHECKING:  # pragma: no cover - typing only
    from typing import TextIO


# v0.25.2: per-session file sinks. ``attach_session_file_sink`` registers
# an append-mode handle keyed by session_id; ``_session_file_sink_processor``
# (installed in the structlog processor chain by :func:`configure`) writes
# every emitted event_dict to the handle whose key matches the event's
# bound ``session_id``. Stdout JSON output is unaffected.
_SESSION_SINKS: dict[str, "TextIO"] = {}


def _session_file_sink_processor(
    logger: Any, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Structlog processor: mirror this event to a per-session JSONL file
    when one is registered for ``event_dict["session_id"]``.

    Best-effort: any I/O error is swallowed so the main log path is never
    broken by a failing sink (full disk, closed handle, etc.).
    """
    sid = event_dict.get("session_id")
    if not isinstance(sid, str) or sid not in _SESSION_SINKS:
        return event_dict
    handle = _SESSION_SINKS[sid]
    try:
        handle.write(json.dumps(event_dict, default=str) + "\n")
        handle.flush()
    except (OSError, TypeError, ValueError):
        # Never block the structlog pipeline on a misbehaving sink.
        pass
    return event_dict


def configure(level: str = "INFO", json_output: bool = True) -> None:
    """Configure stdlib logging and structlog with a shared JSON processor chain.

    v0.25.2: the chain now includes :func:`_session_file_sink_processor`
    so calls to :func:`attach_session_file_sink` take effect immediately
    without requiring a second ``configure``.
    """
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
        _session_file_sink_processor,
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


def file_sink_path(session_id: str, project_root: Path) -> Path:
    """Return ``<project_root>/.autodev/sessions/{session_id}/events.jsonl``.

    Canonical location of the per-session event stream; consumed by
    ``autodev logs`` (v0.25.2).
    """
    return project_root / ".autodev" / "sessions" / session_id / "events.jsonl"


def attach_session_file_sink(session_id: str, project_root: Path) -> None:
    """Open ``events.jsonl`` for ``session_id`` and register the handle.

    From this call onwards, every structlog emission whose bound
    ``session_id`` matches will be appended to the file in addition to
    the standard stdout stream. Idempotent — a second call for the same
    ``session_id`` is a no-op (the existing handle is reused, the file
    is NOT truncated).

    Callers (typically :class:`orchestrator.Orchestrator` after minting
    the session id) are responsible for pairing this with
    :func:`detach_session_file_sink` on shutdown if they want clean
    file-handle cleanup; for short-lived processes that's optional since
    the OS reclaims handles on exit.
    """
    if session_id in _SESSION_SINKS:
        return
    path = file_sink_path(session_id, project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    # touch + open so an empty session file exists even if no logs fire.
    path.touch(exist_ok=True)
    _SESSION_SINKS[session_id] = path.open("a", encoding="utf-8")


def detach_session_file_sink(session_id: str) -> None:
    """Close and unregister the file handle for a session (if any)."""
    handle = _SESSION_SINKS.pop(session_id, None)
    if handle is None:
        return
    try:
        handle.close()
    except OSError:
        pass
