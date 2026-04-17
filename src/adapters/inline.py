"""File-based adapter for running inside an agent session.

When autodev runs inside Claude Code or Cursor, this adapter writes
delegation files to ``.autodev/delegations/`` instead of spawning
subprocesses. The agent reads the delegation, executes the task using
its own tools, writes a response file to ``.autodev/responses/``, and
runs ``autodev resume``.

Tournament judges still use subprocess adapters for independence.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Literal

from adapters.base import PlatformAdapter
from adapters.git_utils import _git_diff
from adapters.inline_types import (
    DelegationPendingSignal,
    InlineResponseError,
    InlineResponseFile,
)
from adapters.types import AgentInvocation, AgentResult, AgentSpec
from autologging import get_logger
from state.paths import delegation_path, response_path

logger = get_logger(__name__)


class InlineAdapter(PlatformAdapter):
    """File-based adapter for running inside an agent session.

    execute() writes a delegation file and raises DelegationPendingSignal.
    The orchestrator catches the signal, suspends, and exits.

    On resume, the orchestrator calls collect_response() directly
    (not execute()) to read the agent's response file.
    """

    name = "inline"

    def __init__(
        self,
        cwd: Path,
        platform_hint: Literal["claude_code", "cursor"] = "claude_code",
    ) -> None:
        self.cwd = Path(cwd)
        self.platform_hint = platform_hint

    # --- PlatformAdapter contract ---

    async def init_workspace(self, cwd: Path, agents: list[AgentSpec]) -> None:
        """Render agent files AND write auto-resume config files."""
        from adapters.inline_config import (
            render_claude_resume_config,
            render_cursor_resume_config,
            update_claude_md,
        )

        # Create delegation/response directories.
        deleg = self.cwd / ".autodev" / "delegations"
        resp = self.cwd / ".autodev" / "responses"
        deleg.mkdir(parents=True, exist_ok=True)
        resp.mkdir(parents=True, exist_ok=True)

        if self.platform_hint == "claude_code":
            agents_dir = cwd / ".claude" / "agents"
            agents_dir.mkdir(parents=True, exist_ok=True)
            claude_md = cwd / ".claude" / "CLAUDE.md"
            section = render_claude_resume_config()
            if claude_md.exists():
                existing = claude_md.read_text(encoding="utf-8")
                content = update_claude_md(existing, section)
            else:
                content = section
            claude_md.write_text(content, encoding="utf-8")

        elif self.platform_hint == "cursor":
            rules_dir = cwd / ".cursor" / "rules"
            rules_dir.mkdir(parents=True, exist_ok=True)
            mdc = rules_dir / "src.mdc"
            mdc.write_text(render_cursor_resume_config(), encoding="utf-8")

        logger.info(
            "inline.init_workspace",
            cwd=str(cwd),
            platform=self.platform_hint,
            agent_count=len(agents),
        )

    async def execute(self, inv: AgentInvocation) -> AgentResult:
        """Write delegation file, then raise DelegationPendingSignal.

        NEVER returns normally — always raises.
        The orchestrator must catch DelegationPendingSignal.
        """
        task_id = inv.metadata.get("task_id", "unknown")
        path = self._write_delegation(inv, task_id)
        logger.info(
            "inline.execute",
            role=inv.role,
            task_id=task_id,
            delegation_path=str(path),
        )
        raise DelegationPendingSignal(
            task_id=task_id,
            role=inv.role,
            delegation_path=path,
        )

    async def parallel(
        self, invs: list[AgentInvocation], max_concurrent: int = 3
    ) -> list[AgentResult]:
        """Inline mode is inherently sequential."""
        raise NotImplementedError(
            "InlineAdapter does not support parallel execution. "
            "Inline mode is inherently sequential."
        )

    async def healthcheck(self) -> tuple[bool, str]:
        """Always healthy — no binary required."""
        return True, "inline adapter (file-based, no binary required)"

    # --- Inline-specific API ---

    def response_path(self, task_id: str, role: str) -> Path:
        """Return the path where the agent should write its response."""
        return response_path(self.cwd, task_id, role)

    def delegation_path(self, task_id: str, role: str) -> Path:
        """Return the path of the delegation file."""
        return delegation_path(self.cwd, task_id, role)

    def has_pending_response(self, task_id: str, role: str) -> bool:
        """Return True if the response file exists."""
        return self.response_path(task_id, role).exists()

    def collect_response(self, task_id: str, role: str) -> AgentResult:
        """Read and validate the response file.

        Called by orchestrator on resume.
        Raises InlineResponseError if file is missing, malformed, or mismatched.
        """
        path = self.response_path(task_id, role)
        if not path.exists():
            raise InlineResponseError(f"response file not found: {path}")
        raw = path.read_text(encoding="utf-8")
        try:
            parsed = InlineResponseFile.model_validate_json(raw)
        except Exception as exc:
            raise InlineResponseError(f"invalid response file {path}: {exc}") from exc

        if parsed.task_id != task_id or parsed.role != role:
            raise InlineResponseError(
                f"response mismatch: expected {task_id}/{role}, "
                f"got {parsed.task_id}/{parsed.role}"
            )

        # If agent didn't provide diff but lists files, compute it.
        diff = parsed.diff
        if diff is None and parsed.files_changed:
            diff = _git_diff(self.cwd)

        return AgentResult(
            success=parsed.success,
            text=parsed.text,
            files_changed=[Path(p) for p in parsed.files_changed],
            diff=diff,
            duration_s=parsed.duration_s,
            error=parsed.error,
        )

    def _write_delegation(self, inv: AgentInvocation, task_id: str) -> Path:
        """Write a delegation file and return its path."""
        resp_path = self.response_path(task_id, inv.role)
        resp_path.parent.mkdir(parents=True, exist_ok=True)
        resp_path_rel = resp_path.relative_to(self.cwd)

        del_path = delegation_path(self.cwd, task_id, inv.role)
        del_path.parent.mkdir(parents=True, exist_ok=True)

        # Build allowed tools list from registry spec if available.
        allowed = inv.allowed_tools or []

        content = _render_delegation_file(
            inv=inv,
            task_id=task_id,
            role=inv.role,
            response_path=resp_path_rel.as_posix(),
            allowed_tools=allowed,
            timeout_s=inv.timeout_s,
        )
        del_path.write_text(content, encoding="utf-8")
        return del_path


def _render_delegation_file(
    *,
    inv: AgentInvocation,
    task_id: str,
    role: str,
    response_path: str,
    allowed_tools: list[str],
    timeout_s: int,
) -> str:
    """Render a delegation file with YAML frontmatter and body."""
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines: list[str] = [
        "---",
        'autodev_version: "1.0"',
        f'task_id: "{task_id}"',
        f'role: "{role}"',
        f'response_path: "{response_path}"',
        f'created_at: "{now}"',
        f"timeout_s: {timeout_s}",
    ]
    if allowed_tools:
        tools_str = ", ".join(f'"{t}"' for t in allowed_tools)
        lines.append(f"allowed_tools: [{tools_str}]")
    lines.append("---")
    lines.append("")
    lines.append(f"# Agent Delegation: {role} / task {task_id}")
    lines.append("")
    lines.append(
        f"You are the **{role}** agent. Complete the task below using your tools."
    )
    lines.append(f"When done, write your response to `{response_path}`")
    lines.append("(see Response Format below), then run `autodev resume` to continue.")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## System Prompt")
    lines.append("")
    lines.append(inv.prompt)
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Response Format")
    lines.append("")
    lines.append(f"Write a JSON file to `{response_path}` with this schema:")
    lines.append("")
    lines.append("```json")
    lines.append("{")
    lines.append('  "schema_version": "1.0",')
    lines.append(f'  "task_id": "{task_id}",')
    lines.append(f'  "role": "{role}",')
    lines.append('  "success": true,')
    lines.append('  "text": "your response text here",')
    lines.append('  "error": null,')
    lines.append('  "duration_s": 0.0,')
    lines.append('  "files_changed": [],')
    lines.append('  "diff": null')
    lines.append("}")
    lines.append("```")
    lines.append("")
    lines.append("Then run: `autodev resume`")
    lines.append("")
    return "\n".join(lines)


__all__ = ["InlineAdapter"]
