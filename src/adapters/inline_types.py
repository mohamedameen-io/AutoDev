"""Types and signals for the inline (file-based) adapter mode.

When autodev runs inside an agent session (Claude Code, Cursor), the
``InlineAdapter`` writes delegation files to ``.autodev/delegations/`` and
raises :class:`DelegationPendingSignal` to suspend the orchestrator. The
agent then reads the delegation, executes the task, writes a response file
to ``.autodev/responses/``, and runs ``autodev resume``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DelegationPendingSignal(Exception):
    """Raised by InlineAdapter.execute() to signal that a delegation file
    has been written and the process should suspend.

    NOT an error — the orchestrator catches this and serializes state.
    """

    def __init__(
        self,
        task_id: str,
        role: str,
        delegation_path: Path,
    ) -> None:
        self.task_id = task_id
        self.role = role
        self.delegation_path = delegation_path
        super().__init__(
            f"Delegation pending: {delegation_path} written, "
            f"awaiting response for {task_id}/{role}"
        )


class InlineSuspendState(BaseModel):
    """Persisted to .autodev/inline-state.json when the process suspends."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    session_id: str
    suspended_at: str
    pending_task_id: str
    pending_role: str
    delegation_path: str
    response_path: str
    orchestrator_step: Literal[
        "developer",
        "reviewer",
        "test_engineer",
        "critic_sounding_board",
        "plan_explorer",
        "plan_domain_expert",
        "plan_architect",
    ]
    retry_count: int = 0
    last_issues: list[str] = Field(default_factory=list)


class InlineResponseFile(BaseModel):
    """Schema for the JSON file the agent writes after completing a delegation."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    task_id: str
    role: str
    success: bool
    text: str
    error: str | None = None
    duration_s: float = 0.0
    files_changed: list[str] = Field(default_factory=list)
    diff: str | None = None


class InlineResponseError(Exception):
    """Raised when the response file is missing, malformed, or mismatched."""

    pass


__all__ = [
    "DelegationPendingSignal",
    "InlineResponseError",
    "InlineResponseFile",
    "InlineSuspendState",
]
