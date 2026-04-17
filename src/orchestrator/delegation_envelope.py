"""Delegation envelope for specialist agent invocation.

Each envelope describes one specialist invocation: who to call, what files
they can touch, what success looks like, and any additional context the
orchestrator wants them to see.

Kept minimal for Phase 4 — we drop the ``commandType`` / ``errorStrategy``
/ ``platformNotes`` fields from the swarm original because autodev doesn't
run inside OpenCode's plugin system.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


DelegationAction = Literal[
    "implement",
    "review",
    "test",
    "explore",
    "critique",
    "consult",
    "document",
    "design",
]


class DelegationEnvelope(BaseModel):
    """Structured task handoff to a specialist role."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    target_agent: str
    action: DelegationAction
    files: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    acceptance: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)

    def render_as_task_message(self) -> str:
        """Return a human-readable text block to prepend to the agent prompt.

        Format is stable — tests assert on mentions of ``task_id`` and the
        ``TASK:`` / ``AGENT:`` / ``ACTION:`` lines.
        """
        lines: list[str] = [
            f"TASK: {self.task_id}",
            f"AGENT: {self.target_agent}",
            f"ACTION: {self.action}",
        ]
        if self.files:
            lines.append("FILES:")
            lines.extend(f"  - {f}" for f in self.files)
        if self.constraints:
            lines.append("CONSTRAINTS:")
            lines.extend(f"  - {c}" for c in self.constraints)
        if self.acceptance:
            lines.append(f"ACCEPTANCE: {self.acceptance}")
        if self.context:
            lines.append("CONTEXT:")
            for k, v in self.context.items():
                lines.append(f"  {k}: {v}")
        return "\n".join(lines)


__all__ = ["DelegationAction", "DelegationEnvelope"]
