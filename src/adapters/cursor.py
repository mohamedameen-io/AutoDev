"""Cursor subprocess adapter (uses `cursor agent --print`)."""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal

from adapters.base import PlatformAdapter
from adapters.git_utils import _diff_files, _git_diff, _git_porcelain_set
from adapters.types import AgentInvocation, AgentResult, AgentSpec
from autologging import get_logger

logger = get_logger(__name__)


# Cursor CLI JSON shape is less documented than Claude's. We parse defensively:
#   - text: prefer "result", fall back to "response", "text", "content"
#   - session_id: prefer "thread_id", fall back to "agent_id", "session_id"
#   - is_error: boolean, default False
# Regardless of shape, raw stdout/stderr are preserved.


_CURSOR_BINARIES = ("cursor", "cursor-agent")


# v0.31.0 (Phase 2.6): wording variants the Cursor CLI / Cursor backend
# uses to communicate either short-window throttling or a hit usage cap.
# All checks are case-insensitive against ``stderr`` AND ``stdout`` (the
# Cursor backend sometimes returns the limit message inside the JSON
# error body on stdout). Adding to either tuple should be paired with a
# unit-test variant in ``tests/test_adapter_cursor.py``.
#
# RATE-LIMITED: short-window throttle (per-minute / burst). Retrying the
# same model after a backoff is plausible, though the current adapter
# downshifts immediately to the more conservative ``auto`` model rather
# than introducing a sleep.
_RATE_LIMIT_PHRASES: tuple[str, ...] = (
    "rate limit",
    "rate_limit",
    "too many requests",
)
# USAGE-LIMIT-HIT: monthly / plan / quota cap. Retrying the same model is
# futile — the only way forward is to downshift (``auto`` + Max Mode
# disabled) and hope the cheaper tier still has headroom on the account.
# Order matters only insofar as the first match wins for telemetry; the
# action is the same either way.
_USAGE_LIMIT_PHRASES: tuple[str, ...] = (
    "usage limit",
    "usage_limit",
    "monthly limit",
    "plan limit",
    "quota exceeded",
    "out of credits",
    "upgrade to continue",
    "limit reached",
)


def _classify_limit_signal(
    stdout: str, stderr: str, returncode: int
) -> Literal["none", "rate_limited", "usage_limit_hit"]:
    """Classify a Cursor CLI failure into a limit subtype, if any.

    The classifier examines both ``stdout`` and ``stderr`` because the
    Cursor backend has been observed to return the limit message inside
    the JSON error body on stdout (with returncode 0 in some shapes,
    non-zero in others) and as plain text on stderr in others. The
    ``returncode == 429`` short-circuit covers the cleanest signal Cursor
    can emit (HTTP 429 surfaced as a process exit code).

    Distinguishing the two subtypes matters for two reasons:

    * Telemetry / forensics — operators looking at ``autodev logs``
      need to tell "the API is throttling me" from "my account ran
      out of monthly credits".
    * Circuit-breaker — both subtypes feed
      :data:`orchestrator.circuit_breaker.INFRASTRUCTURE_SUBTYPES` so
      a sustained stream of either pauses the phase rather than burning
      escalation budget forever.

    Returns ``"none"`` when no limit signal is present (the caller then
    treats the failure as a generic non-zero exit / parse error /
    timeout).
    """
    if returncode == 429:
        # 429 is unambiguous and Cursor sometimes uses it for both subtypes;
        # we still consult the message to refine, defaulting to rate_limited
        # when ambiguous.
        haystack = f"{stdout}\n{stderr}".lower()
        if any(phrase in haystack for phrase in _USAGE_LIMIT_PHRASES):
            return "usage_limit_hit"
        return "rate_limited"

    haystack = f"{stdout}\n{stderr}".lower()
    if any(phrase in haystack for phrase in _USAGE_LIMIT_PHRASES):
        return "usage_limit_hit"
    if any(phrase in haystack for phrase in _RATE_LIMIT_PHRASES):
        return "rate_limited"
    return "none"


def _max_mode_flag_for(max_mode: bool | None) -> list[str]:
    """Translate the tri-state ``max_mode`` field into CLI flags.

    See ``docs/cursor-cli-flags.md`` for the assumption rationale: as of
    the captured Cursor CLI help text, neither ``cursor`` nor
    ``cursor-agent`` exposes a public Max Mode flag (``--max``,
    ``--max-mode``, etc.). The conservative interpretation is that Max
    Mode is opt-in at the IDE / account level and the CLI inherits that
    default. Until/unless Cursor publishes a flag:

    * ``True``  → no-op (we cannot force-enable from the CLI; document
      the gap).
    * ``False`` → no-op (we cannot force-disable from the CLI; the
      downshift to ``--model auto`` is the next-best lever).
    * ``None``  → no-op (default behaviour).

    This helper is the single point to update when a future Cursor
    release exposes a real flag — wire the new spelling here and
    callers automatically benefit.

    TODO(post-cursor-CLI-update): see ``docs/cursor-cli-flags.md`` for
    the verification recipe and update this function once Cursor adds a
    public Max Mode flag.
    """
    # Intentionally returns an empty list today; kept as a function so
    # the call site doesn't grow conditional cruft when the flag lands.
    _ = max_mode  # silence "unused" linter
    return []


# v0.31.0 (Phase 2.6): operator override env var. When set to ``"1"``,
# the usage-limit / rate-limit downshift in ``execute()`` is skipped and
# the underlying error is returned directly. Documented in
# ``docs/cursor-cli-flags.md``.
_DISABLE_FALLBACK_ENV = "AUTODEV_CURSOR_DISABLE_MAX_FALLBACK"


def _fallback_disabled() -> bool:
    return os.environ.get(_DISABLE_FALLBACK_ENV, "") == "1"


class CursorAdapter(PlatformAdapter):
    """Adapter backed by the `cursor agent --print` or `cursor-agent` binary."""

    name = "cursor"

    def __init__(self, binaries: tuple[str, ...] = _CURSOR_BINARIES) -> None:
        self.binaries = binaries

    def _build_command(self, binary: str, inv: AgentInvocation) -> list[str]:
        # Primary `cursor` form: `cursor agent "<prompt>" --print --output-format json`.
        # Fallback `cursor-agent`: same flags, just a different entry binary.
        # `--force` trusts the working directory non-interactively (equivalent to
        # `-f`); without it, recent Cursor Agent versions abort with a
        # "Workspace Trust Required" prompt that can't be answered from a
        # non-TTY subprocess. We intentionally avoid `--yolo`, which would also
        # auto-approve tool calls.
        if binary.endswith("cursor-agent"):
            cmd: list[str] = [
                binary,
                inv.prompt,
                "--print",
                "--output-format",
                "json",
                "--force",
            ]
        else:
            cmd = [
                binary,
                "agent",
                inv.prompt,
                "--print",
                "--output-format",
                "json",
                "--force",
            ]
        if inv.model:
            cmd += ["--model", inv.model]
        # v0.31.0 (Phase 2.6): translate the tri-state ``max_mode`` field
        # into CLI flags. See ``_max_mode_flag_for`` and
        # ``docs/cursor-cli-flags.md`` for current behaviour.
        cmd += _max_mode_flag_for(inv.max_mode)
        if inv.allowed_tools:
            logger.warning(
                "cursor.allowed_tools_ignored",
                role=inv.role,
                allowed_tools=inv.allowed_tools,
                note="cursor has no --allowed-tools; express constraints in .cursor/rules/ (Phase 3)",
            )
        return cmd

    async def init_workspace(self, cwd: Path, agents: list[AgentSpec]) -> None:
        # No-op for Phase 2: the Cursor CLI does not support a workspace
        # configuration file equivalent to `.claude/agents/`. Agent constraints
        # are expressed via `.cursor/rules/` MDC files, which must be authored
        # manually for now.
        # TODO(v0.27+): render `.cursor/rules/<name>.mdc` from AgentSpec via
        # agents.render_cursor to automate rule generation.
        logger.info(
            "cursor.init_workspace_stub",
            cwd=str(cwd),
            agent_count=len(agents),
        )

    async def execute(self, inv: AgentInvocation) -> AgentResult:
        files_before = _git_porcelain_set(inv.cwd)
        start = time.monotonic()

        last_err: str | None = None

        # v0.31.0 (Phase 2.6): unified attempt list. Each entry is a
        # ``(model, max_mode)`` pair. The first attempt mirrors the
        # caller's invocation (``inv.model``, ``inv.max_mode``); if a
        # usage-limit / rate-limit signal arrives we append exactly ONE
        # downshift attempt: ``("auto", False)``. The single-downshift
        # cap stops infinite loops once we are already on the cheapest
        # configuration the CLI exposes — at that point a continued
        # limit signal means the account is genuinely out of headroom
        # and we surface the error.
        attempts: list[tuple[str | None, bool | None]] = [
            (inv.model, inv.max_mode)
        ]
        downshift_used = False

        attempt_idx = 0
        while attempt_idx < len(attempts):
            model, max_mode = attempts[attempt_idx]
            attempt_idx += 1
            limit_subtype: Literal["none", "rate_limited", "usage_limit_hit"] = (
                "none"
            )
            attempt_failure: AgentResult | None = None
            for binary in self.binaries:
                # Create new invocation with this attempt's model + max_mode.
                # Preserve every other field on the original invocation —
                # including ``effort`` — so the downshift retry path doesn't
                # silently drop adapter hints. Cursor's CLI ignores
                # ``--effort`` (claude-specific flag) so the value is plumbed
                # through but unused.
                inv_with_attempt = AgentInvocation(
                    role=inv.role,
                    prompt=inv.prompt,
                    model=model,
                    cwd=inv.cwd,
                    allowed_tools=inv.allowed_tools,
                    timeout_s=inv.timeout_s,
                    max_turns=inv.max_turns,
                    effort=inv.effort,
                    max_mode=max_mode,
                )
                cmd = self._build_command(binary, inv_with_attempt)
                logger.info(
                    "cursor.execute",
                    role=inv.role,
                    model=model,
                    max_mode=max_mode,
                    binary=binary,
                    cwd=str(inv.cwd),
                )
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        cwd=str(inv.cwd),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                except FileNotFoundError as exc:
                    last_err = f"binary not found: {binary}: {exc}"
                    continue

                # ``inv.timeout_s`` is ``int | None`` (per-task overrides may leave
                # it unset). Mirror the claude_code.py:139 guard so ``asyncio.wait_for``
                # always receives a numeric timeout — passing ``None`` is harmless to
                # ``wait_for`` itself but makes the error-message branch below format
                # ``"timeout after Nones"``, and downstream code paths that arithmetic
                # with the value (e.g. circuit breaker windowing) crash on ``None``.
                effective_timeout_s: int = (
                    inv.timeout_s if inv.timeout_s is not None else 600
                )
                try:
                    stdout_b, stderr_b = await asyncio.wait_for(
                        proc.communicate(),
                        timeout=effective_timeout_s,
                    )
                except asyncio.TimeoutError:
                    with suppress(ProcessLookupError):
                        proc.kill()
                    with suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(proc.wait(), timeout=5)
                    duration = time.monotonic() - start
                    return AgentResult(
                        success=False,
                        text="",
                        duration_s=duration,
                        error=f"timeout after {effective_timeout_s}s",
                    )

                duration = time.monotonic() - start
                stdout = stdout_b.decode("utf-8", errors="replace")
                stderr = stderr_b.decode("utf-8", errors="replace")
                returncode = proc.returncode if proc.returncode is not None else -1

                # v0.31.0 (Phase 2.6): unified limit classifier. Looks at
                # both stdout and stderr for the wording variants Cursor
                # uses, distinguishes short-window throttling from a hit
                # usage cap, and feeds the typed subtype into telemetry +
                # the cross-task circuit breaker.
                limit_subtype = _classify_limit_signal(
                    stdout, stderr, returncode
                )
                if limit_subtype != "none":
                    # Tag the failure so the orchestrator's circuit
                    # breaker (``orchestrator.circuit_breaker``) can
                    # roll up a sustained stream of these into a halt.
                    subtype_for_breaker = (
                        "rate_limited"
                        if limit_subtype == "rate_limited"
                        else "usage_limit_hit"
                    )
                    attempt_failure = AgentResult(
                        success=False,
                        text="",
                        duration_s=duration,
                        error=(
                            f"cursor {limit_subtype}: "
                            f"{stderr.strip()[:500] or stdout.strip()[:500]}"
                        ),
                        raw_stdout=stdout,
                        raw_stderr=stderr,
                        subtype=subtype_for_breaker,
                    )
                    # Stop iterating binaries — a limit signal is an
                    # account-level problem, not a missing-binary problem.
                    break

                if returncode != 0:
                    return AgentResult(
                        success=False,
                        text="",
                        duration_s=duration,
                        error=f"cursor exited {returncode}: {stderr.strip()[:500]}",
                        raw_stdout=stdout,
                        raw_stderr=stderr,
                    )

                try:
                    parsed: dict[str, Any] = json.loads(stdout)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "cursor.parse_failed",
                        err=str(exc),
                        raw_stdout=stdout[:500],
                    )
                    return AgentResult(
                        success=False,
                        text="",
                        duration_s=duration,
                        error=f"parse failed: {exc}",
                        raw_stdout=stdout,
                        raw_stderr=stderr,
                    )

                text = _extract_text(parsed)
                is_error = bool(parsed.get("is_error", False))

                files_after = _git_porcelain_set(inv.cwd)
                files_changed = _diff_files(files_before, files_after)
                diff = _git_diff(inv.cwd) if files_changed else None

                return AgentResult(
                    success=not is_error,
                    text=text,
                    tool_calls=[],
                    files_changed=[Path(p) for p in files_changed],
                    diff=diff,
                    duration_s=duration,
                    error=None
                    if not is_error
                    else str(parsed.get("error", "is_error=true")),
                    raw_stdout=stdout,
                    raw_stderr=stderr,
                )

            # End of binary loop for this attempt. Decide whether to
            # downshift, surface the error, or continue (if no binary
            # ran and we're out of attempts the trailing return below
            # handles it).
            if attempt_failure is not None and limit_subtype != "none":
                # Operator override: skip the downshift entirely and
                # return the underlying error. Documented in
                # ``docs/cursor-cli-flags.md``.
                if _fallback_disabled():
                    logger.warning(
                        "cursor.downshift_disabled_by_env",
                        role=inv.role,
                        env_var=_DISABLE_FALLBACK_ENV,
                        trigger_subtype=limit_subtype,
                    )
                    return attempt_failure

                already_on_floor = (model == "auto" and max_mode is False)
                if downshift_used or already_on_floor:
                    # Either we have already used our one downshift
                    # this call OR we're already at the cheapest
                    # configuration — surface the error rather than
                    # looping (Phase 2.6.4 cap).
                    return attempt_failure

                # Append exactly one downshift attempt and continue
                # the outer loop. ``logger.warning`` is the deferred-
                # ledger fallback (the planned ``cursor.model_downshift``
                # ledger op is queued for a follow-up phase that wires
                # adapters into ``PlanManager.ledger_append``; for now
                # the structured log carries the same payload).
                logger.warning(
                    "cursor.model_downshift",
                    role=inv.role,
                    from_model=model,
                    from_max_mode=max_mode,
                    to_model="auto",
                    to_max_mode=False,
                    trigger_subtype=limit_subtype,
                )
                attempts.append(("auto", False))
                downshift_used = True
                continue

        duration = time.monotonic() - start
        return AgentResult(
            success=False,
            text="",
            duration_s=duration,
            error=last_err or "no cursor binary available",
        )

    async def healthcheck(self) -> tuple[bool, str]:
        errors: list[str] = []
        for binary in self.binaries:
            try:
                proc = await asyncio.create_subprocess_exec(
                    binary,
                    "--version",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError as exc:
                errors.append(f"{binary}: not found ({exc})")
                continue
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=5,
                )
            except asyncio.TimeoutError:
                with suppress(ProcessLookupError):
                    proc.kill()
                errors.append(f"{binary}: --version timed out")
                continue
            if proc.returncode == 0:
                return True, (
                    f"{binary}: {stdout_b.decode('utf-8', errors='replace').strip()}"
                )
            errors.append(
                f"{binary}: exit {proc.returncode}: "
                f"{stderr_b.decode('utf-8', errors='replace').strip()[:200]}"
            )
        return False, "; ".join(errors) if errors else "no cursor binary available"


def _extract_text(parsed: dict[str, Any]) -> str:
    """Pick the most likely text field from cursor's JSON output."""
    for key in ("result", "response", "text", "content", "message"):
        value = parsed.get(key)
        if isinstance(value, str) and value:
            return value
    return ""
