"""Claude Code subprocess adapter (uses `claude -p`)."""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from adapters.base import NetworkProbeFailure, PlatformAdapter
from adapters.git_utils import (
    _diff_files,
    _git_diff,
    _git_diff_with_untracked,
    _git_porcelain_set,
)
from adapters.types import AgentInvocation, AgentResult, AgentSpec
from autologging import get_logger
from state.paths import debug_dir

logger = get_logger(__name__)


# v0.31.0 (Phase 1.1): operator switch for the empty-result happy-path
# debug dump. Default ``"1"`` (on) so the next occurrence of the
# "empty reviewer response" failure mode is self-diagnosing — the file
# under ``.autodev/debug/<role>-<ts>-empty.json`` carries the full
# stdout/stderr + invocation context the orchestrator otherwise discards.
# Set ``AUTODEV_DEBUG_RAW_RESPONSES=0`` to disable (e.g. if disk-write
# overhead becomes an operational concern in long runs).
_RAW_RESPONSE_DUMP_ENV = "AUTODEV_DEBUG_RAW_RESPONSES"


def _raw_response_dump_enabled() -> bool:
    return os.environ.get(_RAW_RESPONSE_DUMP_ENV, "1") != "0"


def _api_status_to_subtype(status: int | str) -> str | None:
    """Map a CLI ``api_error_status`` HTTP code to a typed ``subtype``.

    The Claude CLI emits ``api_error_status`` as an integer on
    transport-layer failures but does NOT populate its own ``subtype``
    field for these cases — pre-v0.28 the typed signal was lost into the
    free-text ``result``/``error`` payload. This helper synthesizes a
    subtype the tournament classifier and ledger can reason about:

    ============  =====================
    HTTP status   Synthesized subtype
    ============  =====================
    401, 403      ``auth_failed``
    429           ``rate_limited``
    500-599       ``server_error``
    400-499 *     ``client_error``
    other         ``None``
    ============  =====================

    \\* other than 401/403/429, which are special-cased above.

    Returns ``None`` when ``status`` is missing/non-numeric or falls
    outside the 4xx/5xx ranges so callers can preserve their existing
    ``subtype is None`` semantics.
    """
    try:
        code = int(status)
    except (TypeError, ValueError):
        return None
    if code in (401, 403):
        return "auth_failed"
    if code == 429:
        return "rate_limited"
    if 500 <= code < 600:
        return "server_error"
    if 400 <= code < 500:
        return "client_error"
    return None


# huge-repo (Cluster B1): spawn-agent isolation flags. The target
# repo's ``.claude`` SessionStart hooks + MCP servers inflate every
# ``claude -p`` cold start (seconds added to each spawn + probe). Our
# spawned agents are headless single-shot workers — they receive their
# instructions via ``--prompt`` and their tools via ``--allowed-tools``,
# so they don't need the target's interactive SessionStart hooks or MCP
# servers. We isolate them with:
#   * ``--setting-sources user``            → load ONLY user settings
#     (skips project + local layers → skips project-defined SessionStart
#     hooks).
#   * ``--strict-mcp-config --mcp-config '{"mcpServers":{}}'`` → force
#     zero MCP servers regardless of the target's ``.mcp.json``.
# Deliberately NOT ``--bare`` (that would also drop the target's
# ``CLAUDE.md``, which we WANT to keep so agents follow repo
# conventions; ``CLAUDE.md`` is not a settings source, so it still
# loads). Verified against ``claude --help`` (flags present in the
# installed CLI).
_ISOLATION_FLAGS: tuple[str, ...] = (
    "--setting-sources",
    "user",
    "--strict-mcp-config",
    "--mcp-config",
    '{"mcpServers":{}}',
)


def _isolation_flags(adapters_cfg: Any | None) -> list[str]:
    """Return the spawn-agent isolation flags, or ``[]`` when disabled.

    Gated by ``adapters_cfg.suppress_target_repo_config`` (default
    ``True``). The default holds **even when ``adapters_cfg`` is
    ``None``** (unbound), so the detect-time / unbound probe is isolated
    too — that is the key cold-start win on huge target repos. Operators
    can opt out by setting ``suppress_target_repo_config=False``, which
    yields ``[]`` → a command byte-identical to pre-isolation.
    """
    if getattr(adapters_cfg, "suppress_target_repo_config", True):
        return list(_ISOLATION_FLAGS)
    return []


# Observed `claude -p --output-format json` shape (claude 2.1.92):
#   {"type":"result","subtype":"success","is_error":false,"duration_ms":...,
#    "num_turns":...,"result":"...","stop_reason":"end_turn",
#    "session_id":"<uuid>","total_cost_usd":...,"usage":{...},
#    "modelUsage":{...},"permission_denials":[],"terminal_reason":"completed",
#    "uuid":"<uuid>"}
# The `claude` CLI does NOT accept `--cwd`; we use the subprocess cwd param.


class ClaudeCodeAdapter(PlatformAdapter):
    """Adapter backed by the `claude -p` binary."""

    name = "claude_code"

    def __init__(self, binary: str = "claude") -> None:
        self.binary = binary
        # v0.10.0: most recently spawned subprocess PID. Read by the
        # tournament's per-pass adaptive ratcheting to feed
        # :func:`runtime.resource_probe.measure_subprocess_rss`. Defaults
        # to ``None`` until the first ``execute`` call lands. With
        # concurrent calls, this is the PID of the *last subprocess
        # whose ``create_subprocess_exec`` resolved* — by design a
        # single-slot pulse, not a per-call list. The downstream RSS
        # probe is robust to dead PIDs (returns ``None`` on
        # ``NoSuchProcess`` / ``AccessDenied``), so a stale-by-the-time-
        # we-read-it value gracefully degrades to "no measurement this
        # pass" rather than a crash.
        self.last_pid: int | None = None
        # v0.36.0 F2: optional adapters config (probe retry knobs).
        # Set via :meth:`bind_adapters_cfg` after construction; the
        # adapter is created by ``get_adapter()`` before the config is
        # loaded in some call paths, so binding is decoupled from
        # construction. ``None`` means "use defaults".
        self._adapters_cfg: Any | None = None
        # huge-repo (Cluster B0/B2): the full ``AutodevConfig`` and the
        # repo cwd, threaded by ``get_adapter(cfg=...)`` so the probe can
        # huge-repo-scale ``probe_timeout_s`` via the H5 resolver. Both
        # ``None`` until bound; ``None`` means "no scaling, use the base
        # ``probe_timeout_s``" (small-repo / unbound default behaviour).
        self._root_cfg: Any | None = None
        self._probe_cwd: Path | None = None

    def bind_adapters_cfg(
        self,
        cfg: Any,
        *,
        root_cfg: Any | None = None,
        probe_cwd: Path | None = None,
    ) -> None:
        """v0.36.0 F2: attach the loaded ``cfg.adapters`` block.

        Used by orchestrator wiring to give the adapter access to
        :class:`AdaptersConfig` (probe retry attempts / backoff)
        without coupling adapter construction to the config loader.

        huge-repo (Cluster B0): the optional ``root_cfg`` /
        ``probe_cwd`` kwargs additionally give the probe access to the
        full :class:`config.schema.AutodevConfig` and the repository
        root so ``probe_timeout_s`` can be huge-repo-scaled via the H5
        resolver. They default ``None`` so the legacy positional
        single-arg call (``bind_adapters_cfg(cfg.adapters)``) stays
        valid and unbound callers keep small-repo defaults.
        """
        self._adapters_cfg = cfg
        if root_cfg is not None:
            self._root_cfg = root_cfg
        if probe_cwd is not None:
            self._probe_cwd = probe_cwd

    def _build_command(self, inv: AgentInvocation) -> list[str]:
        cmd: list[str] = [
            self.binary,
            "-p",
            inv.prompt,
            "--output-format",
            "json",
            "--permission-mode",
            "acceptEdits",
        ]
        if inv.model:
            cmd += ["--model", inv.model]
        if inv.max_turns and inv.max_turns > 0:
            cmd += ["--max-turns", str(inv.max_turns)]
        # ``--effort {low,medium,high,xhigh,max}`` controls test-time compute.
        # ``None`` or empty string → flag omitted, the CLI inherits the
        # user-global default from ``~/.claude/settings.json``.
        if inv.effort:
            cmd += ["--effort", inv.effort]
        if inv.allowed_tools:
            cmd += ["--allowed-tools", ",".join(inv.allowed_tools)]
        # huge-repo (Cluster B1): isolate the spawned agent from the
        # target repo's SessionStart hooks + MCP servers (cold-start
        # win). Default on; ``suppress_target_repo_config=False`` →
        # ``[]`` → command byte-identical to pre-isolation.
        cmd += _isolation_flags(self._adapters_cfg)
        # NOTE: We deliberately do NOT pass `--continue`; every call is fresh.
        # ``inv.max_mode`` (v0.31.0 Phase 2.6) is a Cursor-specific tri-state.
        # Claude Code has no Max Mode equivalent, so this adapter intentionally
        # does not consume the field.
        return cmd

    async def init_workspace(self, cwd: Path, agents: list[AgentSpec]) -> None:
        # No-op: the claude CLI receives all agent instructions via the
        # `--prompt` flag passed directly to the subprocess in `_build_command`.
        # There is no workspace configuration file for `claude -p` to pick up,
        # so nothing needs to be written here.
        # TODO(v0.27+): render `.claude/agents/<name>.md` from AgentSpec via
        # agents.render_claude to support persistent sub-agent configurations.
        logger.info(
            "claude_code.init_workspace_stub",
            cwd=str(cwd),
            agent_count=len(agents),
        )

    async def execute(self, inv: AgentInvocation) -> AgentResult:
        cmd = self._build_command(inv)
        # ``inv.timeout_s`` became ``int | None`` in v0.8.0 to support per-task
        # complexity overrides resolved at the orchestrator boundary. Apply the
        # adapter-level default (600s) when the field is ``None`` — the legacy
        # behavior pre-dating the type change.
        effective_timeout_s: int = inv.timeout_s if inv.timeout_s is not None else 600
        logger.info(
            "claude_code.execute",
            role=inv.role,
            model=inv.model,
            max_turns=inv.max_turns,
            allowed_tools=inv.allowed_tools,
            cwd=str(inv.cwd),
        )
        files_before = _git_porcelain_set(inv.cwd)
        start = time.monotonic()
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(inv.cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            # v0.10.0: record the spawned PID for downstream RSS probing.
            # Set BEFORE communicate() so we capture it even if the call
            # times out or fails (per-pass probing wants peak-time PIDs,
            # not just success-path ones).
            self.last_pid = proc.pid
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=effective_timeout_s,
                )
            except asyncio.CancelledError:
                # v0.23.0 C3: parent task cancelled (SIGTERM, KeyboardInterrupt
                # propagation, or asyncio.gather cancel). Kill the child so
                # we don't leak ``claude -p`` processes after the orchestrator
                # exits — D-6 finding from the 2026-05-09 Unity stall.
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
                # Drain whatever the subprocess buffered before kill so the
                # dump captures the partial transcript (mirrors the rc!=0
                # path added in v0.5.2). Best-effort: a hung process whose
                # streams never resolve will TimeoutError out at 2.0s.
                stdout_b = b""
                stderr_b = b""
                with suppress(Exception):
                    stdout_b, stderr_b = await asyncio.wait_for(
                        proc.communicate(), timeout=2.0
                    )
                duration = time.monotonic() - start
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
        except FileNotFoundError as exc:
            duration = time.monotonic() - start
            return AgentResult(
                success=False,
                text="",
                duration_s=duration,
                error=f"claude binary not found: {exc}",
            )

        duration = time.monotonic() - start
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        returncode = proc.returncode if proc.returncode is not None else -1

        if returncode != 0:
            err_tail = stderr.strip()[:500]
            out_tail = stdout.strip()[:500]
            if not err_tail and not out_tail:
                msg = f"claude exited {returncode} with empty stderr"
            elif err_tail:
                msg = f"claude exited {returncode}: {err_tail}"
            else:
                msg = f"claude exited {returncode} (stdout): {out_tail}"

            # Fix 4 completion: deterministic-subtype failures (e.g.
            # ``error_max_turns``, ``error_max_tokens``,
            # ``error_during_execution``) sometimes exit rc=1 yet write a
            # complete result JSON to stdout. Extract ``subtype``
            # opportunistically so the tournament retry layer's
            # ``_DETERMINISTIC_SUBTYPES`` short-circuit fires and the call
            # is not retried. Falls through to ``None`` on empty / malformed
            # / non-dict stdout — preserving the genuine-subprocess-death
            # path (which DOES want to retry via the transient-substring
            # classifier).
            subtype_val: str | None = None
            api_error_status_val: int | None = None
            try:
                parsed_failure = json.loads(stdout)
                if isinstance(parsed_failure, dict):
                    st = parsed_failure.get("subtype")
                    if st:
                        subtype_val = str(st)
                    # v0.28.0 (Bug 1): surface ``api_error_status`` and
                    # synthesize a typed subtype from it when the CLI
                    # itself didn't classify the failure. A real error
                    # subtype (e.g. ``error_max_turns``) wins; the CLI's
                    # placeholder ``"success"`` paired with ``is_error=true``
                    # does NOT win — synthesis fills it in.
                    raw_status = parsed_failure.get("api_error_status")
                    if raw_status is not None:
                        try:
                            api_error_status_val = int(raw_status)
                        except (TypeError, ValueError):
                            api_error_status_val = None
                    is_err_failure = bool(parsed_failure.get("is_error", False))
                    if (
                        (
                            subtype_val is None
                            or (is_err_failure and subtype_val == "success")
                        )
                        and raw_status is not None
                    ):
                        synthesized = _api_status_to_subtype(raw_status)
                        if synthesized is not None:
                            subtype_val = synthesized
            except (json.JSONDecodeError, TypeError):
                pass

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
                error=msg,
                raw_stdout=stdout,
                raw_stderr=stderr,
                subtype=subtype_val,
                api_error_status=api_error_status_val,
            )

        try:
            parsed: dict[str, Any] = json.loads(stdout)
        except json.JSONDecodeError as exc:
            logger.warning(
                "claude_code.parse_failed",
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

        text = str(parsed.get("result", ""))
        is_error = bool(parsed.get("is_error", False))

        # v0.31.0 (Phase 1.1): empty-result happy-path dump. The CLI
        # exited 0 and produced parseable JSON, but ``result`` is empty
        # (or whitespace). Without this branch the orchestrator soft-
        # blocks the task with ``["empty reviewer response"]`` and there
        # is no on-disk artifact to diagnose root cause (Hypothesis A:
        # max_tokens; B: parser swallowed prose; C: schema rejected
        # envelope). Dump full stdout/stderr + invocation context, then
        # return a failure-flagged result so downstream retry / escalate
        # logic kicks in. Gated by ``AUTODEV_DEBUG_RAW_RESPONSES``.
        #
        # v0.31.1 (Phase 0): drop the ``not is_error`` guard. The CLI
        # routinely emits ``is_error=true`` alongside ``result=""`` on
        # transport-layer failures (per the v0.28.0 comment elsewhere
        # in this file), and that is exactly the case the dump was
        # built to capture. Empty text is the machinery-failure signal;
        # ``is_error`` is orthogonal to whether the forensic dump
        # should be written. The orchestrator still classifies
        # ``is_error=true`` correctly for control flow downstream.
        if not text.strip():
            if _raw_response_dump_enabled():
                self._dump_empty_result(
                    inv=inv,
                    stdout=stdout,
                    stderr=stderr,
                    duration=duration,
                )
            # huge-repo (Cluster B4): synthesize the infra subtype from
            # ``api_error_status`` exactly as the happy path (below) and
            # the rc!=0 path (above) already do. This makes an empty
            # result retryable ONLY when an infra status (429 / 5xx) was
            # present — the existing ``_classify_error`` maps
            # ``rate_limited`` / ``server_error`` to retryable. A genuine
            # empty result (no status) keeps ``subtype=None`` so the
            # hard-fail behaviour is preserved (we do NOT add "empty
            # result" to the transient-substring list).
            empty_subtype_val: str | None = (
                str(parsed.get("subtype") or "") or None
            )
            empty_api_status_val: int | None = None
            raw_status = parsed.get("api_error_status")
            if raw_status is not None:
                try:
                    empty_api_status_val = int(raw_status)
                except (TypeError, ValueError):
                    empty_api_status_val = None
                if (
                    empty_subtype_val is None
                    or (is_error and empty_subtype_val == "success")
                ):
                    synthesized = _api_status_to_subtype(raw_status)
                    if synthesized is not None:
                        empty_subtype_val = synthesized
            return AgentResult(
                success=False,
                text="",
                duration_s=duration,
                error="empty result from CLI",
                raw_stdout=stdout,
                raw_stderr=stderr,
                subtype=empty_subtype_val,
                api_error_status=empty_api_status_val,
            )
        # Surface the CLI's ``subtype`` field on the result so the tournament
        # retry layer can short-circuit deterministic failures (e.g.
        # ``error_max_turns``). Empty / missing → None.
        subtype_val = parsed.get("subtype") or None
        if subtype_val is not None:
            subtype_val = str(subtype_val)

        # v0.28.0 (Bug 1): surface ``api_error_status`` and, when the CLI
        # didn't classify the failure itself, synthesize a typed subtype
        # from the HTTP code. Mirrors the rc!=0 branch above so a 403
        # surfaces the same ``auth_failed`` signal regardless of which
        # exit path the CLI took. When ``is_error=true`` the CLI's own
        # ``"success"`` subtype is treated as a placeholder (the CLI
        # routinely emits ``subtype="success"`` alongside ``is_error=true``
        # on transport-layer failures); a real error subtype like
        # ``error_max_turns`` still wins over synthesis.
        api_error_status_val = None
        raw_status = parsed.get("api_error_status")
        if raw_status is not None:
            try:
                api_error_status_val = int(raw_status)
            except (TypeError, ValueError):
                api_error_status_val = None
        if (
            (subtype_val is None or (is_error and subtype_val == "success"))
            and is_error
            and raw_status is not None
        ):
            synthesized = _api_status_to_subtype(raw_status)
            if synthesized is not None:
                subtype_val = synthesized

        cost_usd: float = 0.0
        if "total_cost_usd" in parsed:
            cost_usd = float(parsed["total_cost_usd"])
        else:
            logger.warning(
                "claude_code.missing_total_cost_usd",
                role=inv.role,
            )

        files_after = _git_porcelain_set(inv.cwd)
        files_changed = _diff_files(files_before, files_after)
        # v0.22.1 A5: ``_git_diff`` (``git diff HEAD``) omits untracked
        # files; use the sibling helper that splices per-untracked
        # ``--no-index`` blocks so evidence captures new-file work.
        # Pre-A5 every developer task creating new files (notes/, etc.)
        # had ``evidence.diff = null`` despite ``files_changed`` being
        # populated — D-3 finding from the 2026-05-09 Unity stall.
        diff = _git_diff_with_untracked(inv.cwd) if files_changed else None

        result = AgentResult(
            success=not is_error,
            text=text,
            tool_calls=[],  # TODO(v0.27+): parse tool_calls from claude-code stream-json output for an audit trail; today we keep it empty (no current consumer).
            files_changed=[Path(p) for p in files_changed],
            diff=diff,
            duration_s=duration,
            cost_usd=cost_usd,
            error=None if not is_error else str(parsed.get("error", "is_error=true")),
            raw_stdout=stdout,
            raw_stderr=stderr,
            subtype=subtype_val,
            api_error_status=api_error_status_val,
        )
        return result

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

        Filename: ``{role}-{unix_ts_ms}.txt`` — Windows-safe (no ``:``),
        pass-num-orderable, role-grouped on ``ls``. On any OSError (permission,
        readonly fs, etc.) we log a warning and swallow — never let a debug-dump
        failure mask the original subprocess error.
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
                "claude_code.debug_dump_failed",
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
        """v0.31.0 (Phase 1.1): dump full context for an empty-result happy
        path to ``.autodev/debug/{role}-{ts}-empty.json``.

        Distinct from :meth:`_dump_failure_transcript` (txt format, fired on
        rc!=0 / timeout) so operators can ``ls .autodev/debug/*-empty.json``
        to count occurrences of this specific failure mode without grepping
        through transcripts. Best-effort: any OSError is logged and
        swallowed — never let a debug-dump failure mask the empty-result
        signal the caller already converted into a soft block upstream.
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
                "claude_code.empty_result_dump_failed",
                role=inv.role,
                err=str(exc),
            )

    async def healthcheck(self) -> tuple[bool, str]:
        """Two-stage probe: cheap ``--version``, then live PONG round-trip.

        Stage 1 (``claude --version``) catches "CLI missing / broken install".
        Stage 2 (``echo PONG | claude -p --max-turns 1``, 10s timeout) catches
        bad auth and network failures that stage 1 cannot see — a perfectly
        installed CLI with an expired token still returns 0 from
        ``--version`` but fails the live call with HTTP 401/403.

        Reason prefixes (return value's second element):
          * ``"binary not found: ..."``       — Stage 1 ``FileNotFoundError``.
          * ``"claude --version exit ..."``   — Stage 1 nonzero.
          * ``"claude --version timed out"``  — Stage 1 hang (5s).
          * ``"auth_failed: ..."``            — Stage 2 ``is_error=true`` with
                                                401/403 in the message.
          * ``"network: ..."``                — Stage 2 timeout (10s) or other
                                                non-auth ``is_error=true``.
        """
        # Stage 1: cheap CLI presence + version probe.
        try:
            proc = await asyncio.create_subprocess_exec(
                self.binary,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return False, f"binary not found: {self.binary}"
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(),
                timeout=5,
            )
        except asyncio.CancelledError:
            # v0.23.0 C3: kill child on parent cancel.
            with suppress(ProcessLookupError):
                proc.kill()
            with suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=2)
            raise
        except asyncio.TimeoutError:
            with suppress(ProcessLookupError):
                proc.kill()
            return False, "claude --version timed out"
        if proc.returncode != 0:
            return False, (
                f"claude --version exit {proc.returncode}: "
                f"{stderr_b.decode('utf-8', errors='replace').strip()[:200]}"
            )
        version_str = stdout_b.decode("utf-8", errors="replace").strip()

        # Stage 2: live PONG probe. Mirrors ``execute()``'s subprocess
        # patterns (lines 112-117) — same create_subprocess_exec/wait_for
        # idioms; same kill-on-cancel/timeout discipline.
        return await self._pong_probe(version_str)

    async def _pong_probe(self, version_str: str) -> tuple[bool, str]:
        """Run ``echo PONG | claude -p --max-turns 1`` and classify the result.

        10s per-attempt timeout is intentional: this runs at every
        ``autodev resume`` / ``execute`` startup; a longer wait would
        defeat fail-fast semantics.

        v0.36.0 F2: retry on transient network failures with exponential
        backoff (1s, 3s, 9s by default). Auth failures short-circuit
        and do NOT consume retry budget. After the final attempt fails
        the method raises :class:`NetworkProbeFailure` with the last
        error message and a remediation suggestion. Operators that
        want the legacy ``(False, "network: ...")`` shape should
        configure ``probe_retry_attempts=1`` — the loop still runs
        once, the raise still fires, and CLI fallback code keeps the
        legacy string match via ``last_error``.
        """
        # Resolve retry knobs from config; degrade gracefully when the
        # adapter is exercised outside a real autodev config (unit
        # tests bind a minimal stub).
        _adapters_cfg = getattr(self, "_adapters_cfg", None)
        attempts = int(
            getattr(_adapters_cfg, "probe_retry_attempts", 3)
            if _adapters_cfg is not None
            else 3
        )
        backoff_initial = float(
            getattr(_adapters_cfg, "probe_backoff_initial_s", 1.0)
            if _adapters_cfg is not None
            else 1.0
        )
        if attempts < 1:
            attempts = 1

        # huge-repo (Cluster B2): resolve the per-attempt probe timeout.
        # Base from ``cfg.adapters.probe_timeout_s`` (default 10.0 when
        # unbound). Then huge-repo-scale it via the H5 resolver when the
        # full cfg + cwd are bound (``probe_timeout_s`` multiplier 1.5 →
        # 15s on huge repos, beating the ~7-10s cold start). The whole
        # scaling path is wrapped in a defensive ``try/except`` that
        # falls back to the base on ANY error — the probe must NEVER
        # crash (it runs before the orchestrator exists, at every
        # startup).
        timeout_s = float(
            getattr(_adapters_cfg, "probe_timeout_s", 10.0)
            if _adapters_cfg is not None
            else 10.0
        )
        if self._root_cfg is not None and self._probe_cwd is not None:
            try:
                from orchestrator.huge_repo_overrides import (  # noqa: PLC0415
                    resolve_huge_repo_value,
                )

                effective, _mult = resolve_huge_repo_value(
                    key="probe_timeout_s",
                    base_value=timeout_s,
                    cwd=self._probe_cwd,
                    cfg=self._root_cfg,
                )
                timeout_s = float(effective)
            except Exception:  # noqa: BLE001 — probe must never crash
                pass  # keep the base timeout

        last_error = ""
        for i in range(attempts):
            ok, msg = await self._pong_probe_once(version_str, timeout_s=timeout_s)
            if ok:
                return True, msg
            # Auth failures short-circuit. The credential isn't going to
            # become valid in 1, 3, or 9 seconds — retrying would just
            # waste startup latency.
            if msg.startswith("auth_failed:"):
                return False, msg
            last_error = msg
            # Best-effort ledger emission — captures probe failures even
            # on retry-success runs so operators can track flaky network
            # baselines. Final-attempt op is emitted after the loop.
            try:
                from autologging import get_logger as _gl  # noqa: PLC0415

                _gl(__name__).warning(
                    "claude_code._pong_probe.retry",
                    attempt=i + 1,
                    of=attempts,
                    err=msg,
                )
            except Exception:  # noqa: BLE001
                pass
            if i < attempts - 1:
                # Backoff: 1s, 3s, 9s for the default config.
                await asyncio.sleep(backoff_initial * (3 ** i))

        # All attempts failed → raise structured exception. The CLI
        # catch site renders ``.suggestion`` + exits with code 5; the
        # legacy ``(False, "network: ...")`` shape stays observable via
        # ``last_error`` for callers that haven't migrated.
        raise NetworkProbeFailure(
            adapter="claude_code",
            attempts=attempts,
            last_error=last_error,
            suggestion=(
                "Check VPN / proxy / adapter health. The probe runs "
                f"with a {timeout_s:g}s per-attempt timeout and retried "
                f"{attempts} times before giving up."
            ),
        )

    async def _pong_probe_once(
        self, version_str: str, *, timeout_s: float = 10.0
    ) -> tuple[bool, str]:
        """Single PONG round-trip without retry/backoff.

        ``timeout_s`` defaults to 10.0 (the historical hardcoded value);
        :meth:`_pong_probe` resolves a configured / huge-repo-scaled
        value and passes it explicitly. huge-repo (Cluster B1): the
        probe command carries the same isolation flags as the agent
        command (``_isolation_flags``) — this is the highest-leverage
        cold-start fix, since the original workaround disabled MCP
        precisely to speed up the probe.
        """
        probe_cmd: list[str] = [
            self.binary,
            "-p",
            "PONG",
            "--max-turns",
            "1",
            "--output-format",
            "json",
            *_isolation_flags(self._adapters_cfg),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *probe_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            # Should be unreachable — Stage 1 already proved the binary
            # exists — but stay defensive for racy filesystems.
            return False, f"binary not found: {self.binary}"
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_s,
            )
        except asyncio.CancelledError:
            with suppress(ProcessLookupError):
                proc.kill()
            with suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=2)
            raise
        except asyncio.TimeoutError:
            with suppress(ProcessLookupError):
                proc.kill()
            with suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=2)
            return False, f"network: PONG probe timed out after {timeout_s:g}s"

        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        rc = proc.returncode if proc.returncode is not None else -1

        # Try to parse the structured JSON. The CLI emits its result envelope
        # to stdout even on rc!=0 in many failure modes (see Fix 4 comment in
        # ``execute`` above), so don't gate on rc first.
        parsed: dict[str, Any] | None = None
        try:
            candidate = json.loads(stdout)
            if isinstance(candidate, dict):
                parsed = candidate
        except (json.JSONDecodeError, TypeError):
            parsed = None

        if parsed is not None:
            is_error = bool(parsed.get("is_error", False))
            if not is_error:
                return True, version_str or "ok"
            message = str(parsed.get("result", "") or parsed.get("error", "")).strip()
            # Auth-failure detection: HTTP 401/403 in the CLI's message body.
            # The Anthropic CLI surfaces these as ``API Error: 401`` /
            # ``API Error: 403`` strings inside the ``result`` field.
            if "401" in message or "403" in message:
                snippet = message[:200] if message else "401/403"
                return False, f"auth_failed: {snippet}"
            snippet = message[:200] if message else f"is_error=true rc={rc}"
            return False, f"network: {snippet}"

        # Unparseable stdout — treat as a network/transport failure rather
        # than success. Surface a short tail so operators can diagnose.
        tail = stderr.strip()[:200] or stdout.strip()[:200] or f"rc={rc}"
        return False, f"network: PONG probe produced no parseable result ({tail})"
