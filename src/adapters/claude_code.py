"""Claude Code subprocess adapter (uses `claude -p`)."""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from adapters.base import PlatformAdapter
from adapters.git_utils import _diff_files, _git_diff, _git_porcelain_set
from adapters.types import AgentInvocation, AgentResult, AgentSpec
from autologging import get_logger
from state.paths import debug_dir

logger = get_logger(__name__)


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
        if inv.allowed_tools:
            cmd += ["--allowed-tools", ",".join(inv.allowed_tools)]
        # NOTE: We deliberately do NOT pass `--continue`; every call is fresh.
        return cmd

    async def init_workspace(self, cwd: Path, agents: list[AgentSpec]) -> None:
        # No-op: the claude CLI receives all agent instructions via the
        # `--prompt` flag passed directly to the subprocess in `_build_command`.
        # There is no workspace configuration file for `claude -p` to pick up,
        # so nothing needs to be written here.
        # TODO(phase-3): render `.claude/agents/<name>.md` from AgentSpec via
        # agents.render_claude to support persistent sub-agent configurations.
        logger.info(
            "claude_code.init_workspace_stub",
            cwd=str(cwd),
            agent_count=len(agents),
        )

    async def execute(self, inv: AgentInvocation) -> AgentResult:
        cmd = self._build_command(inv)
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
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=inv.timeout_s,
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
                    error=f"timeout after {inv.timeout_s}s",
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
        # Surface the CLI's ``subtype`` field on the result so the tournament
        # retry layer can short-circuit deterministic failures (e.g.
        # ``error_max_turns``). Empty / missing → None.
        subtype_val = parsed.get("subtype") or None
        if subtype_val is not None:
            subtype_val = str(subtype_val)

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
        diff = _git_diff(inv.cwd) if files_changed else None

        result = AgentResult(
            success=not is_error,
            text=text,
            tool_calls=[],  # TODO(phase-3+): parse from stream-json if needed
            files_changed=[Path(p) for p in files_changed],
            diff=diff,
            duration_s=duration,
            cost_usd=cost_usd,
            error=None if not is_error else str(parsed.get("error", "is_error=true")),
            raw_stdout=stdout,
            raw_stderr=stderr,
            subtype=subtype_val,
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

    async def healthcheck(self) -> tuple[bool, str]:
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
        except asyncio.TimeoutError:
            with suppress(ProcessLookupError):
                proc.kill()
            return False, "claude --version timed out"
        if proc.returncode != 0:
            return False, (
                f"claude --version exit {proc.returncode}: "
                f"{stderr_b.decode('utf-8', errors='replace').strip()[:200]}"
            )
        return True, stdout_b.decode("utf-8", errors="replace").strip()
