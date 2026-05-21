"""Platform auto-detection for adapter selection.

v0.26.0: ``inline`` is no longer a valid platform — the file-based
delegation adapter was removed. The schema migrator in
``src/config/schema.py`` rewrites legacy ``platform: inline`` configs
to ``platform: claude_code`` (with a ``DeprecationWarning``) so callers
that resolve a config-loaded ``platform`` field still pass through here
cleanly.

v0.37.0 H4: trigger-context routing. When ``autodev`` is invoked from
inside a Claude Code session (``CLAUDECODE=1`` /
``CLAUDE_PROJECT_DIR``) the ``claude_code`` adapter is selected
automatically; from a Cursor terminal (``TERM_PROGRAM=Cursor`` /
``CURSOR_*``) the ``cursor`` adapter is selected. Explicit
``--platform X`` always wins; the
:attr:`config.schema.AutodevConfig.adapter_respect_trigger_context`
escape hatch (mapped onto ``respect_trigger_context`` here) restores
pre-v0.37.0 behaviour.
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


def _detect_trigger_context() -> PlatformName | None:
    """Return the platform implied by the invoking host's env, or ``None``.

    Claude-context wins if both somehow co-occur (e.g. nested shells)
    because the Claude Code session is the actively-driving agent in
    that case.
    """
    if os.environ.get("CLAUDECODE") == "1" or os.environ.get("CLAUDE_PROJECT_DIR"):
        return "claude_code"
    if os.environ.get("TERM_PROGRAM") == "Cursor" or any(
        k.startswith("CURSOR_") for k in os.environ
    ):
        return "cursor"
    return None


async def detect_platform(
    preferred: _PreferredName = "auto",
    *,
    cwd: Path | None = None,
    respect_trigger_context: bool = True,
) -> PlatformName:
    """Return the platform name to use.

    Precedence:
      1. If `preferred` != "auto": return it (after healthcheck).
      2. v0.37.0 H4: trigger-context env detection. When
         ``respect_trigger_context`` (default True) and
         :func:`_detect_trigger_context` resolves a host, healthcheck
         that adapter; on success return it, on failure log a warning
         and fall through.
      3. Env var `AUTODEV_PLATFORM` if set and valid.
      4. v0.31.0 (Phase 5.5): if both adapters healthcheck and
         ``AUTODEV_LANG_WEIGHT`` > 0 and ``cwd`` is provided, factor the
         language fitness score into the choice. Default weight is 0.0,
         so the historical Claude-bias is preserved for backward
         compatibility.
      5. Try `claude --version`; if ok -> "claude_code".
      6. Try `cursor --version`; if ok -> "cursor".
      7. Raise `AdapterError`.
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
        logger.info(
            "detect_platform.selected",
            platform=preferred,
            details=details,
            source="preferred",
        )
        return preferred  # type: ignore[return-value]

    # v0.37.0 H4: trigger-context routing (between explicit preferred
    # and AUTODEV_PLATFORM env). Healthcheck the chosen adapter; on
    # failure, fall through to the env / fitness / fallback path.
    if respect_trigger_context:
        trigger = _detect_trigger_context()
        if trigger is not None:
            trigger_adapter = _make_adapter(trigger)
            ok, details = await trigger_adapter.healthcheck()
            if ok:
                logger.info(
                    "detect_platform.trigger_context_detected",
                    host=trigger,
                    chose=trigger,
                    details=details,
                    source="trigger_context",
                )
                logger.info(
                    "detect_platform.selected",
                    platform=trigger,
                    details=details,
                    source="trigger_context",
                )
                return trigger
            logger.warning(
                "detect_platform.trigger_context_unhealthy",
                host=trigger,
                details=details,
            )

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
        logger.info(
            "detect_platform.selected",
            platform=env,
            details=details,
            source="env",
        )
        return env  # type: ignore[return-value]

    # v0.31.0 (Phase 5.5): language-weighted selection (opt-in).
    # v0.37.0 H5: on huge repos, default the weight to 0.5 so the
    # fitness-weighted path engages by default (operator can still
    # override via the env var). Trigger-context (H4) precedence above
    # already short-circuited if applicable, so this only affects the
    # fallback/auto path. Tested in
    # ``tests/test_adapter_detect_huge_repo_weight.py``.
    env_weight = os.environ.get("AUTODEV_LANG_WEIGHT")
    if env_weight is not None:
        try:
            weight = float(env_weight)
        except ValueError:
            weight = 0.0
    elif cwd is not None:
        try:
            from orchestrator.repo_size import is_huge_repo

            weight = 0.5 if is_huge_repo(cwd) else 0.0
        except Exception:  # noqa: BLE001 — defensive: never block on probe
            weight = 0.0
    else:
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
                logger.info(
                    "detect_platform.selected",
                    platform=selected,
                    details=f"fitness {score:.0f}",
                    source="fitness",
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
            "detect_platform.selected",
            platform="claude_code",
            details=claude_details,
            source="fallback",
        )
        return "claude_code"

    cursor = CursorAdapter()
    ok, details = await cursor.healthcheck()
    if ok:
        logger.info(
            "detect_platform.selected",
            platform="cursor",
            details=details,
            source="fallback",
        )
        return "cursor"

    raise AdapterError("No platform CLI found; install `claude` or `cursor` and log in")


async def get_adapter(
    platform: _PreferredName = "auto",
    cwd: Path | None = None,
    platform_hint: Literal["claude_code", "cursor"] | None = None,
    respect_trigger_context: bool = True,
) -> PlatformAdapter:
    """Resolve a PlatformAdapter instance for the given preference.

    v0.31.0 (Phase 5.5): ``cwd`` is forwarded into :func:`detect_platform`
    so the language-weighted auto-selection (``AUTODEV_LANG_WEIGHT > 0``)
    has a repo to scan. Default behaviour (``weight == 0``) is unchanged.

    v0.37.0 H4: ``respect_trigger_context`` mirrors
    :attr:`config.schema.AutodevConfig.adapter_respect_trigger_context`;
    callers in ``src/cli/`` thread it from ``cfg`` so the operator-facing
    escape hatch works without re-reading config inside the adapter
    layer.
    """
    name = await detect_platform(
        platform, cwd=cwd, respect_trigger_context=respect_trigger_context
    )
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
