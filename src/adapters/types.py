"""Pydantic types shared across platform adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ToolCall(BaseModel):
    """A single tool invocation reported by an adapter.

    Phase 2 adapters do not populate this from `--output-format json` (which
    exposes only the final aggregated result). Populating this list is a
    future enhancement (stream-json parsing).
    """

    model_config = ConfigDict(extra="forbid")

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    result_summary: str | None = None
    error: str | None = None


class AgentInvocation(BaseModel):
    """Input to `PlatformAdapter.execute`."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    role: str
    prompt: str
    cwd: Path
    model: str | None = None
    timeout_s: int = 600
    allowed_tools: list[str] | None = None
    max_turns: int = 1
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentResult(BaseModel):
    """Output of `PlatformAdapter.execute`."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    success: bool
    text: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    files_changed: list[Path] = Field(default_factory=list)
    diff: str | None = None
    duration_s: float
    error: str | None = None
    raw_stdout: str = ""
    raw_stderr: str = ""


class AgentSpec(BaseModel):
    """Definition of an agent (used by `init_workspace` in Phase 3)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    prompt: str
    tools: list[str] = Field(default_factory=list)
    model: str | None = None


class StreamEvent(BaseModel):
    """Reserved for future stream-json parsing; unused in Phase 2."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["tool_start", "tool_end", "text", "error"]
    data: dict[str, Any] = Field(default_factory=dict)
