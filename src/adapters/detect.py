"""Platform auto-detection for adapter selection.

v0.26.0: ``inline`` is no longer a valid platform — the file-based
delegation adapter was removed. The schema migrator in
``src/config/schema.py`` rewrites legacy ``platform: inline`` configs
to ``platform: claude_code`` (with a ``DeprecationWarning``) so callers
that resolve a config-loaded ``platform`` field still pass through here
cleanly.
"""

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


PlatformName = Literal["claude_code", "cursor"]
_PreferredName = Literal["claude_code", "cursor", "auto"]
_VALID_PLATFORMS = ("claude_code", "cursor")


async def detect_platform(
    preferred: _PreferredName = "auto",
    *,
    cwd: Path | None = None,
) -> PlatformName:
    """Return the platform name to use.

    Precedence:
      1. If `preferred` != "auto": return it (after healthcheck).
      2. Env var `AUTODEV_PLATFORM` if set and valid.
      3. v0.31.0 (Phase 5.5): if both adapters healthcheck and
         ``AUTODEV_LANG_WEIGHT`` > 0 and ``cwd`` is provided, factor the
         language fitness score into the choice. Default weight is 0.0,
         so the historical Claude-bias is preserved for backward
         compatibility.
      4. Try `claude --version`; if ok -> "claude_code".
      5. Try `cursor --version`; if ok -> "cursor".
      6. Raise `AdapterError`.
    """
    if preferred not in ("claude_code", "cursor", "auto"):
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

    # v0.31.0 (Phase 5.5): language-weighted selection (opt-in).
    try:
        weight = float(os.environ.get("AUTODEV_LANG_WEIGHT", "0.0"))
    except ValueError:
        weight = 0.0

    claude = ClaudeCodeAdapter()
    claude_ok, claude_details = await claude.healthcheck()

    if weight > 0.0 and cwd is not None and claude_ok:
        # Probe Cursor too so we can pick the higher fitness score.
        cursor = CursorAdapter()
        cursor_ok, cursor_details = await cursor.healthcheck()
        if cursor_ok:
            try:
                from adapters.fitness import compute_fitness_score
                from runtime.language_profile import compute_language_profile, top_n

                profile = compute_language_profile(cwd)
                claude_score = compute_fitness_score("claude_code", profile)
                cursor_score = compute_fitness_score("cursor", profile)
                if cursor_score > claude_score:
                    selected: PlatformName = "cursor"
                    score = cursor_score
                else:
                    selected = "claude_code"
                    score = claude_score
                top = ", ".join(
                    f"{lang} {share:.0%}" for lang, share in top_n(profile, 3)
                )
                logger.info(
                    "detect_platform.selected_by_fitness",
                    platform=selected,
                    score=score,
                    weight=weight,
                    profile=profile,
                )
                # Surface the bias to the operator via stderr so it shows
                # up in CLI output, not just structured logs.
                import sys as _sys

                print(
                    f"Selected '{selected}' "
                    f"(fitness {score:.0f}; codebase profile: {top})",
                    file=_sys.stderr,
                )
                return selected
            except Exception:  # noqa: BLE001 - never block on profile failure
                pass

    if claude_ok:
        logger.info(
            "detect_platform.selected", platform="claude_code", details=claude_details
        )
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
    """Resolve a PlatformAdapter instance for the given preference.

    v0.31.0 (Phase 5.5): ``cwd`` is forwarded into :func:`detect_platform`
    so the language-weighted auto-selection (``AUTODEV_LANG_WEIGHT > 0``)
    has a repo to scan. Default behaviour (``weight == 0``) is unchanged.
    """
    name = await detect_platform(platform, cwd=cwd)
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
    raise AdapterError(f"unknown platform: {name!r}")
