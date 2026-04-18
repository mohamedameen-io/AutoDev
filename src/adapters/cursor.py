"""Cursor subprocess adapter (uses `cursor agent --print`)."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

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


class CursorAdapter(PlatformAdapter):
    """Adapter backed by the `cursor agent --print` or `cursor-agent` binary."""

    name = "cursor"

    def __init__(self, binaries: tuple[str, ...] = _CURSOR_BINARIES) -> None:
        self.binaries = binaries

    def _build_command(self, binary: str, inv: AgentInvocation) -> list[str]:
        # Primary `cursor` form: `cursor agent "<prompt>" --print --output-format json`.
        # Fallback `cursor-agent`: same flags, just a different entry binary.
        if binary.endswith("cursor-agent"):
            cmd: list[str] = [binary, inv.prompt, "--print", "--output-format", "json"]
        else:
            cmd = [binary, "agent", inv.prompt, "--print", "--output-format", "json"]
        if inv.model:
            cmd += ["--model", inv.model]
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
        # TODO(phase-3): render `.cursor/rules/<name>.mdc` from AgentSpec via
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

        # For important roles, try explicit model first, fallback to auto on rate limit
        fallback_models: dict[str, str] = {}
        if inv.model in ("opus", "sonnet"):
            fallback_models[inv.model] = "auto"

        # Try primary model first, then fallback. When no model is set, iterate
        # once with model=None so the binary runs without a --model flag.
        models_to_try: list[str | None] = [inv.model] if inv.model else [None]
        if inv.model and inv.model in fallback_models:
            models_to_try.append(fallback_models[inv.model])

        for model in models_to_try:
            for binary in self.binaries:
                # Create new invocation with this model
                inv_with_model = AgentInvocation(
                    role=inv.role,
                    prompt=inv.prompt,
                    model=model,
                    cwd=inv.cwd,
                    allowed_tools=inv.allowed_tools,
                    timeout_s=inv.timeout_s,
                )
                cmd = self._build_command(binary, inv_with_model)
                logger.info(
                    "cursor.execute",
                    role=inv.role,
                    model=model,
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

                duration = time.monotonic() - start
                stdout = stdout_b.decode("utf-8", errors="replace")
                stderr = stderr_b.decode("utf-8", errors="replace")
                returncode = proc.returncode if proc.returncode is not None else -1

                # Check for rate limit errors - fall back to auto if available
                if (
                    returncode == 429
                    or "rate limit" in stderr.lower()
                    or "rate_limit" in stderr.lower()
                ):
                    if (
                        model in fallback_models
                        and fallback_models[model] not in models_to_try
                    ):
                        logger.warning(
                            "cursor.rate_limit_fallback",
                            role=inv.role,
                            from_model=model,
                            to_model=fallback_models[model],
                        )
                        models_to_try.append(fallback_models[model])
                        break  # Try next model in list
                    else:
                        return AgentResult(
                            success=False,
                            text="",
                            duration_s=duration,
                            error=f"rate limit exhausted: {stderr.strip()[:500]}",
                            raw_stdout=stdout,
                            raw_stderr=stderr,
                        )

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
