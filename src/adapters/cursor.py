"""Cursor subprocess adapter (uses `cursor agent --print`)."""

from __future__ import annotations

import asyncio
import datetime as _dt
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
from state.paths import debug_dir

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


# v0.31.0 (Phase 1.1): mirror of the claude_code adapter switch. Default
# ``"1"`` (on) so empty-result happy paths leave a forensic artifact on
# disk. Set ``AUTODEV_DEBUG_RAW_RESPONSES=0`` to disable. Both adapters
# read the same env var so operators have one knob, not two.
_RAW_RESPONSE_DUMP_ENV = "AUTODEV_DEBUG_RAW_RESPONSES"


def _raw_response_dump_enabled() -> bool:
    return os.environ.get(_RAW_RESPONSE_DUMP_ENV, "1") != "0"


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

        # v0.31.0 (Phase 2.3): track binaries that have already failed
        # ``FileNotFoundError`` within THIS execute() call. There is no
        # point retrying a missing binary across the inner downshift
        # loop — the filesystem state isn't going to change between
        # attempts. Per-process binary-availability caching (across
        # calls) is structurally invasive (would need a class-level
        # cache + invalidation hooks) and is deferred — see
        # ``docs/critical_analysis/`` Phase 2.3 follow-up.
        unavailable_binaries: set[str] = set()

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
                # v0.31.0 (Phase 2.3): skip binaries that already
                # FileNotFoundError'd in an earlier attempt of this call.
                if binary in unavailable_binaries:
                    continue
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
                    # v0.31.0 (Phase 1.4): preserve the output-token
                    # hint across the downshift retry so the reviewer
                    # call doesn't silently revert to CLI default
                    # mid-loop. Cursor adapter ignores the field today
                    # (no public flag) — see ``AgentInvocation`` docstring.
                    output_token_budget=inv.output_token_budget,
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
                    # v0.31.0 (Phase 2.3): mark this binary as
                    # unavailable for the remainder of the call so the
                    # downshift retry doesn't re-probe a binary the
                    # filesystem just told us is missing.
                    unavailable_binaries.add(binary)
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
                except asyncio.CancelledError:
                    # v0.31.0 (Phase 2.1): parent task cancelled (SIGTERM,
                    # KeyboardInterrupt propagation, or asyncio.gather
                    # cancel). Kill the in-flight cursor child so we don't
                    # leak processes after the orchestrator exits — mirrors
                    # claude_code.py:171-180. Re-raise WITHOUT iterating to
                    # the next binary / next downshift attempt: cancellation
                    # is terminal for this call.
                    with suppress(ProcessLookupError):
                        proc.kill()
                    with suppress(Exception):
                        await asyncio.wait_for(proc.wait(), timeout=5)
                    raise
                except asyncio.TimeoutError:
                    with suppress(ProcessLookupError):
                        proc.kill()
                    with suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(proc.wait(), timeout=5)
                    duration = time.monotonic() - start
                    # v0.31.0 (Phase 2.2): capture forensics for every
                    # failure mode, including timeouts. Drain whatever the
                    # subprocess buffered before kill (best-effort, 2s cap)
                    # so the dump captures any partial transcript.
                    stdout_b = b""
                    stderr_b = b""
                    with suppress(Exception):
                        stdout_b, stderr_b = await asyncio.wait_for(
                            proc.communicate(), timeout=2.0
                        )
                    stdout = (
                        stdout_b.decode("utf-8", errors="replace")
                        if stdout_b
                        else ""
                    )
                    stderr = (
                        stderr_b.decode("utf-8", errors="replace")
                        if stderr_b
                        else ""
                    )
                    self._dump_failure_transcript(
                        inv=inv,
                        stdout=stdout,
                        stderr=stderr,
                        returncode=-1,  # sentinel: timeout
                        duration=duration,
                    )
                    return AgentResult(
                        success=False,
                        text="",
                        duration_s=duration,
                        error=f"timeout after {effective_timeout_s}s",
                        raw_stdout=stdout,
                        raw_stderr=stderr,
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
                    # v0.31.0 (Phase 2.2): forensics dump on every
                    # non-zero exit (mirrors claude_code.py:290-296).
                    self._dump_failure_transcript(
                        inv=inv,
                        stdout=stdout,
                        stderr=stderr,
                        returncode=returncode,
                        duration=duration,
                    )
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
                    # v0.31.0 (Phase 2.2): forensics dump on parse
                    # failure — the raw stdout is critical evidence for
                    # diagnosing CLI shape drift.
                    self._dump_failure_transcript(
                        inv=inv,
                        stdout=stdout,
                        stderr=stderr,
                        returncode=returncode,
                        duration=duration,
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

                # v0.31.0 (Phase 1.1): empty-result happy-path dump.
                # Same logic as claude_code adapter — the CLI exited 0 +
                # produced parseable JSON, but every text-bearing field
                # is empty. Persist a forensic artifact under
                # ``.autodev/debug/<role>-<ts>-empty.json`` and surface
                # the call as a typed failure so the orchestrator's
                # retry / escalate FSM kicks in instead of the silent
                # soft-block on ``["empty reviewer response"]``.
                #
                # v0.31.1 (Phase 0): drop the ``not is_error`` guard.
                # When the CLI emits ``is_error=true`` alongside an
                # empty result (transport-layer failures, timeouts),
                # the dump is exactly what we need to diagnose root
                # cause. Empty text is the machinery-failure signal;
                # ``is_error`` is orthogonal.
                if not text.strip():
                    if _raw_response_dump_enabled():
                        self._dump_empty_result(
                            inv=inv,
                            stdout=stdout,
                            stderr=stderr,
                            duration=duration,
                        )
                    return AgentResult(
                        success=False,
                        text="",
                        duration_s=duration,
                        error="empty result from CLI",
                        raw_stdout=stdout,
                        raw_stderr=stderr,
                    )

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
                    # v0.31.0 (Phase 2.2): downshift is being skipped,
                    # so this IS a terminal failure — dump forensics.
                    self._dump_failure_transcript(
                        inv=inv,
                        stdout=attempt_failure.raw_stdout or "",
                        stderr=attempt_failure.raw_stderr or "",
                        returncode=-1,
                        duration=attempt_failure.duration_s,
                    )
                    return attempt_failure

                already_on_floor = (model == "auto" and max_mode is False)
                if downshift_used or already_on_floor:
                    # Either we have already used our one downshift
                    # this call OR we're already at the cheapest
                    # configuration — surface the error rather than
                    # looping (Phase 2.6.4 cap).
                    # v0.31.0 (Phase 2.2): terminal failure — dump
                    # forensics. Note: the FIRST limit signal on the
                    # initial attempt does NOT dump (the downshift
                    # might still recover); only the cap-hit case does.
                    self._dump_failure_transcript(
                        inv=inv,
                        stdout=attempt_failure.raw_stdout or "",
                        stderr=attempt_failure.raw_stderr or "",
                        returncode=-1,
                        duration=attempt_failure.duration_s,
                    )
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
        # v0.31.0 (Phase 2.2): terminal "no binary anywhere" failure —
        # dump forensics. There is no subprocess output to capture, but
        # the meta block (role / model / cwd / timestamp) is still
        # diagnostic for "why did the call never reach a CLI?".
        self._dump_failure_transcript(
            inv=inv,
            stdout="",
            stderr=last_err or "no cursor binary available",
            returncode=-1,
            duration=duration,
        )
        return AgentResult(
            success=False,
            text="",
            duration_s=duration,
            error=last_err or "no cursor binary available",
        )

    def _dump_failure_transcript(
        self,
        *,
        inv: AgentInvocation,
        stdout: str,
        stderr: str,
        returncode: int,
        duration: float,
    ) -> None:
        """Best-effort: dump full subprocess context to ``.autodev/debug/``.

        Mirrors :meth:`adapters.claude_code.ClaudeCodeAdapter._dump_failure_transcript`
        line-for-line so operators see the same forensic format regardless
        of which adapter produced the failure. Filename:
        ``{role}-{unix_ts_ms}.txt`` — Windows-safe (no ``:``),
        pass-num-orderable, role-grouped on ``ls``. On any OSError
        (permission, readonly fs, etc.) we log a warning and swallow —
        never let a debug-dump failure mask the original subprocess error.
        """
        try:
            target_dir = debug_dir(inv.cwd)
            target_dir.mkdir(parents=True, exist_ok=True)
            ts_ms = int(time.time() * 1000)
            target = target_dir / f"{inv.role}-{ts_ms}.txt"
            iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
            allowed_tools_repr = (
                ",".join(inv.allowed_tools) if inv.allowed_tools else ""
            )
            sections = (
                "== meta ==\n"
                f"role: {inv.role}\n"
                f"model: {inv.model or ''}\n"
                f"max_turns: {inv.max_turns}\n"
                f"allowed_tools: {allowed_tools_repr}\n"
                f"returncode: {returncode}\n"
                f"duration_s: {duration:.3f}\n"
                f"timestamp: {iso}\n"
                "\n== prompt ==\n"
                f"{inv.prompt}\n"
                "\n== stdout ==\n"
                f"{stdout}\n"
                "\n== stderr ==\n"
                f"{stderr}\n"
            )
            target.write_text(sections, encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "cursor.debug_dump_failed",
                role=inv.role,
                err=str(exc),
            )

    def _dump_empty_result(
        self,
        *,
        inv: AgentInvocation,
        stdout: str,
        stderr: str,
        duration: float,
    ) -> None:
        """v0.31.0 (Phase 1.1): empty-result dump for the Cursor adapter.

        Mirrors :meth:`adapters.claude_code.ClaudeCodeAdapter._dump_empty_result`
        line-for-line so operators see one consistent forensic format
        regardless of which adapter produced the empty happy-path
        result. See that docstring for the full rationale. Best-effort —
        any OSError is logged and swallowed.
        """
        try:
            target_dir = debug_dir(inv.cwd)
            target_dir.mkdir(parents=True, exist_ok=True)
            ts_ms = int(time.time() * 1000)
            target = target_dir / f"{inv.role}-{ts_ms}-empty.json"
            iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
            payload = {
                "note": (
                    "empty result on happy path; investigate prompt size, "
                    "max_tokens, structured-output schema"
                ),
                "role": inv.role,
                "model": inv.model or "",
                "max_turns": inv.max_turns,
                "prompt_size_bytes": len(inv.prompt.encode("utf-8")),
                "duration_s": round(duration, 3),
                "timestamp": iso,
                "raw_stdout": stdout,
                "raw_stderr": stderr,
            }
            target.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning(
                "cursor.empty_result_dump_failed",
                role=inv.role,
                err=str(exc),
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
            except asyncio.CancelledError:
                # v0.31.0 (Phase 2.1): kill child on parent cancel —
                # mirrors claude_code.py:476-482.
                with suppress(ProcessLookupError):
                    proc.kill()
                with suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=2)
                raise
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
