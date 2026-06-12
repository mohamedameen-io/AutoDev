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
from collections.abc import Iterable
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


# v0.38.0 HK9: explicit allowlist replaces the v0.37.0
# ``startswith("CURSOR_")`` heuristic which over-matched on shell rc
# files like ``CURSOR_RC_FILE`` that have nothing to do with Cursor IDE
# trigger context. Operators on newer Cursor versions can extend this
# via ``cfg.cursor_trigger_env_extra`` without waiting for a release.
_CURSOR_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "CURSOR_TRACE_ID",
        "CURSOR_AGENT",
        "CURSOR_VERSION",
        "CURSOR_AGENT_ID",
    }
)


def _detect_trigger_context(
    *, extra_cursor_env: Iterable[str] = (),
) -> PlatformName | None:
    """Return the platform implied by the invoking host's env, or ``None``.

    Claude-context wins if both somehow co-occur (e.g. nested shells)
    because the Claude Code session is the actively-driving agent in
    that case.

    v0.38.0 HK9: Cursor detection now uses an explicit allowlist
    (``_CURSOR_ENV_ALLOWLIST``) unioned with the operator-supplied
    ``extra_cursor_env`` (drawn from
    :attr:`config.schema.AutodevConfig.cursor_trigger_env_extra`) instead
    of the prefix-match heuristic — the prefix match was prone to
    false positives on shell rc state.
    """
    if os.environ.get("CLAUDECODE") == "1" or os.environ.get("CLAUDE_PROJECT_DIR"):
        return "claude_code"
    cursor_keys = _CURSOR_ENV_ALLOWLIST | set(extra_cursor_env)
    if os.environ.get("TERM_PROGRAM") == "Cursor" or any(
        k in cursor_keys for k in os.environ
    ):
        return "cursor"
    return None


def _maybe_warn_multiplexer(
    *,
    respect_trigger_context: bool,
    extra_cursor_env: Iterable[str] = (),
) -> None:
    """v0.38.0 HK8: log a single warning when running under tmux / GNU
    screen AND a trigger-context env is present.

    Multiplexers inherit envs across nested shells and mangle
    ``TERM_PROGRAM``, which is the root cause of the most common
    "AutoDev picked the wrong adapter" support tickets. The warning is
    purely diagnostic — no behaviour change — so v0.39 retrospectives
    can decide whether to add a hard precedence rule.
    """
    if not respect_trigger_context:
        return
    tmux = os.environ.get("TMUX")
    sty = os.environ.get("STY")
    if not (tmux or sty):
        return
    trigger = _detect_trigger_context(extra_cursor_env=extra_cursor_env)
    if trigger is None:
        return
    if tmux and sty:
        multiplexer = "BOTH"
    elif tmux:
        multiplexer = "TMUX"
    else:
        multiplexer = "STY"
    logger.warning(
        "detect_platform.tmux_screen_detected",
        multiplexer=multiplexer,
        trigger=trigger,
    )


async def detect_platform(
    preferred: _PreferredName = "auto",
    *,
    cwd: Path | None = None,
    respect_trigger_context: bool = True,
    cursor_trigger_env_extra: Iterable[str] = (),
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

    v0.38.0 HK9: ``cursor_trigger_env_extra`` extends the built-in
    Cursor env allowlist (``_CURSOR_ENV_ALLOWLIST``). Caller threads
    :attr:`config.schema.AutodevConfig.cursor_trigger_env_extra`.

    v0.38.0 HK8: emits a single ``detect_platform.tmux_screen_detected``
    warning when running under a terminal multiplexer AND a trigger
    context is present, so v0.39 retrospectives can quantify how often
    multiplexer state interferes with adapter selection. No behaviour
    change — pure diagnostic.
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

    # v0.38.0 HK8: terminal-multiplexer diagnostic. tmux / GNU screen
    # mangle TERM_PROGRAM and inherit envs across nested shells, which
    # turns out to be the most common source of "AutoDev picked the
    # wrong adapter" support tickets. Forensics-only: log the
    # multiplexer + trigger combo so v0.39 retrospectives can decide
    # whether to add a behaviour change here.
    _maybe_warn_multiplexer(
        respect_trigger_context=respect_trigger_context,
        extra_cursor_env=cursor_trigger_env_extra,
    )

    # v0.37.0 H4: trigger-context routing (between explicit preferred
    # and AUTODEV_PLATFORM env). Healthcheck the chosen adapter; on
    # failure, fall through to the env / fitness / fallback path.
    if respect_trigger_context:
        trigger = _detect_trigger_context(
            extra_cursor_env=cursor_trigger_env_extra,
        )
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


def _classify_selection_source(
    preferred: _PreferredName,
    *,
    respect_trigger_context: bool,
    cursor_trigger_env_extra: Iterable[str] = (),
) -> tuple[str, bool]:
    """v0.38.0 HK10: derive ``(source, trigger_context_detected)`` for
    the :func:`get_adapter` selection-meta payload.

    Sources mirror the ``source=`` tag on the
    ``detect_platform.selected`` structured-log line:

    * ``"preferred"`` — explicit ``--platform X`` was passed.
    * ``"trigger_context"`` — Claude Code / Cursor host env was the
      deciding factor.
    * ``"env"`` — ``AUTODEV_PLATFORM`` env beat the fallback.
    * ``"fitness"`` — language-fitness scoring picked between two
      healthy adapters.
    * ``"fallback"`` — first adapter that healthchecks.

    The classifier mirrors the precedence in :func:`detect_platform`
    but does NOT re-run the healthcheck — it answers "which arm did
    the dispatcher take", forensics-only. The
    ``trigger_context_detected`` flag is independent of ``source`` so
    operators can spot the "host context was present but didn't win"
    case (e.g. Cursor host, but ``--platform claude_code`` override).
    """
    trigger_detected = (
        _detect_trigger_context(extra_cursor_env=cursor_trigger_env_extra)
        is not None
    )
    if preferred != "auto":
        return "preferred", trigger_detected
    if respect_trigger_context and trigger_detected:
        return "trigger_context", trigger_detected
    if os.environ.get("AUTODEV_PLATFORM"):
        return "env", trigger_detected
    # Fitness vs fallback distinction depends on AUTODEV_LANG_WEIGHT
    # and is best-effort here (the actual classifier inside
    # detect_platform also factors huge-repo defaults). The "fitness"
    # tag is only emitted when AUTODEV_LANG_WEIGHT was non-zero AND
    # both adapters healthchecked — neither knowable without running
    # the probe. Default to "fallback" so the forensics op never
    # reports a false fitness selection.
    return "fallback", trigger_detected


async def get_adapter(
    platform: _PreferredName = "auto",
    cwd: Path | None = None,
    platform_hint: Literal["claude_code", "cursor"] | None = None,
    respect_trigger_context: bool = True,
    cursor_trigger_env_extra: Iterable[str] = (),
    cfg: object | None = None,
) -> tuple[PlatformAdapter, dict[str, object]]:
    """Resolve a PlatformAdapter instance for the given preference.

    v0.31.0 (Phase 5.5): ``cwd`` is forwarded into :func:`detect_platform`
    so the language-weighted auto-selection (``AUTODEV_LANG_WEIGHT > 0``)
    has a repo to scan. Default behaviour (``weight == 0``) is unchanged.

    v0.37.0 H4: ``respect_trigger_context`` mirrors
    :attr:`config.schema.AutodevConfig.adapter_respect_trigger_context`;
    callers in ``src/cli/`` thread it from ``cfg`` so the operator-facing
    escape hatch works without re-reading config inside the adapter
    layer.

    v0.38.0 HK9: ``cursor_trigger_env_extra`` threads
    :attr:`config.schema.AutodevConfig.cursor_trigger_env_extra` into
    the trigger-context detector.

    v0.38.0 HK10 (breaking change): returns ``(adapter, selection_meta)``
    where ``selection_meta = {"platform", "source", "trigger_context_detected",
    "healthcheck_ok"}``. CLI callers append this to the ledger as the
    ``adapter_selected`` op so post-mortems can correlate "which
    selection arm fired this session" with downstream behaviour.

    huge-repo (Cluster B0): ``cfg`` is the loaded
    :class:`config.schema.AutodevConfig`. When provided and the built
    adapter exposes ``bind_adapters_cfg``, this binds ``cfg.adapters``
    (probe retry / timeout knobs) plus the full ``cfg`` and ``cwd`` so
    the mandatory post-``get_adapter`` re-probe in the CLI commands
    picks up the configured / huge-repo-scaled probe timeout. The
    detect-time probe inside :func:`detect_platform` stays unbound (it
    runs on a throwaway adapter at the 20s unbound default — a fast "is
    the CLI alive?" check that, post-v0.39.0, pins the fast probe model
    and gets 20s of headroom so a slow huge-repo cold start doesn't fail
    it). Backward-compatible: ``cfg=None`` (the default) skips binding
    entirely.
    """
    name = await detect_platform(
        platform,
        cwd=cwd,
        respect_trigger_context=respect_trigger_context,
        cursor_trigger_env_extra=cursor_trigger_env_extra,
    )
    source, trigger_detected = _classify_selection_source(
        platform,
        respect_trigger_context=respect_trigger_context,
        cursor_trigger_env_extra=cursor_trigger_env_extra,
    )
    adapter = _make_adapter(name, cwd=cwd, platform_hint=platform_hint)
    # huge-repo (Cluster B0): bind the loaded config onto the adapter so
    # the existing probe knobs (``probe_retry_attempts`` /
    # ``probe_backoff_initial_s``) and the new ``probe_timeout_s``
    # (+ huge-repo scaling) actually reach the mandatory re-probe. Before
    # this, ``bind_adapters_cfg`` was never called in production and the
    # knobs silently fell back to defaults.
    if cfg is not None and hasattr(adapter, "bind_adapters_cfg"):
        adapters_cfg = getattr(cfg, "adapters", None)
        adapter.bind_adapters_cfg(adapters_cfg, root_cfg=cfg, probe_cwd=cwd)
    selection_meta: dict[str, object] = {
        "platform": name,
        "source": source,
        "trigger_context_detected": trigger_detected,
        # detect_platform already ran a healthcheck for whichever arm
        # actually returned, so ``healthcheck_ok`` is implied True
        # here — a failed probe raises AdapterError upstream.
        "healthcheck_ok": True,
    }
    return adapter, selection_meta


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
