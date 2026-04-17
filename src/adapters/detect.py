"""Platform auto-detection for adapter selection."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from adapters.base import PlatformAdapter
from adapters.claude_code import ClaudeCodeAdapter
from adapters.cursor import CursorAdapter
from errors import AdapterError
from autologging import get_logger

logger = get_logger(__name__)


PlatformName = Literal["claude_code", "cursor", "inline"]
_PreferredName = Literal["claude_code", "cursor", "inline", "auto"]
_VALID_PLATFORMS = ("claude_code", "cursor", "inline")


async def detect_platform(preferred: _PreferredName = "auto") -> PlatformName:
    """Return the platform name to use.

    Precedence:
      1. If `preferred` != "auto": return it (after healthcheck).
      2. Env var `AUTODEV_PLATFORM` if set and valid.
      3. Try `claude --version`; if ok -> "claude_code".
      4. Try `cursor --version`; if ok -> "cursor".
      5. Raise `AdapterError`.
    """
    if preferred not in ("claude_code", "cursor", "inline", "auto"):
        raise AdapterError(f"invalid preferred platform: {preferred!r}")

    if preferred != "auto":
        adapter = _make_adapter(preferred)
        ok, details = await adapter.healthcheck()
        if not ok:
            raise AdapterError(
                f"preferred platform {preferred!r} unavailable: {details}"
            )
        return preferred  # type: ignore[return-value]

    env = os.environ.get("AUTODEV_PLATFORM")
    if env:
        if env not in _VALID_PLATFORMS:
            raise AdapterError(
                f"AUTODEV_PLATFORM={env!r} is invalid; "
                f"expected one of {_VALID_PLATFORMS}"
            )
        adapter = _make_adapter(env)
        ok, details = await adapter.healthcheck()
        if not ok:
            raise AdapterError(
                f"AUTODEV_PLATFORM={env!r} set but unavailable: {details}"
            )
        return env  # type: ignore[return-value]

    claude = ClaudeCodeAdapter()
    ok, details = await claude.healthcheck()
    if ok:
        logger.info("detect_platform.selected", platform="claude_code", details=details)
        return "claude_code"

    cursor = CursorAdapter()
    ok, details = await cursor.healthcheck()
    if ok:
        logger.info("detect_platform.selected", platform="cursor", details=details)
        return "cursor"

    raise AdapterError("No platform CLI found; install `claude` or `cursor` and log in")


async def get_adapter(
    platform: _PreferredName = "auto",
    cwd: Path | None = None,
    platform_hint: Literal["claude_code", "cursor"] | None = None,
) -> PlatformAdapter:
    """Resolve a PlatformAdapter instance for the given preference."""
    name = await detect_platform(platform)
    return _make_adapter(name, cwd=cwd, platform_hint=platform_hint)


def _make_adapter(
    name: str,
    cwd: Path | None = None,
    platform_hint: Literal["claude_code", "cursor"] | None = None,
) -> PlatformAdapter:
    if name == "claude_code":
        return ClaudeCodeAdapter()
    if name == "cursor":
        return CursorAdapter()
    if name == "inline":
        from adapters.inline import InlineAdapter

        resolved_cwd = cwd if cwd is not None else Path.cwd()
        return InlineAdapter(
            cwd=resolved_cwd,
            platform_hint=platform_hint or "claude_code",
        )
    raise AdapterError(f"unknown platform: {name!r}")
