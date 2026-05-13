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
    allowed_tools: list[str] | None = None
    max_turns: int = 1
    # Per-invocation Claude Code ``--effort`` hint. Plain ``str`` (not
    # ``Literal``) because the adapter accepts any value for forward-compat
    # with new effort levels Claude Code may add. The ``Literal`` validation
    # lives at the config layer (see :class:`config.schema.AgentConfig`).
    # ``None`` = the adapter omits the ``--effort`` flag and inherits the
    # user-global default in ``~/.claude/settings.json``.
    effort: str | None = None
    # Per-invocation subprocess timeout in seconds. Adapters consume this
    # via ``asyncio.wait_for(..., timeout=inv.timeout_s)`` after applying a
    # default fallback when ``None`` (e.g. 600s in the Claude Code adapter).
    # v0.8.0 added per-task scaling via :func:`tournament.task_overrides
    # .resolve_task_timeout_s` keyed off ``Task.complexity``; the resolver
    # returns ``None`` when no override applies and the caller falls back
    # to its own default (e.g. ``_DEFAULT_DEVELOPER_TIMEOUT_S``).
    timeout_s: int | None = None
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
    cost_usd: float = 0.0
    error: str | None = None
    raw_stdout: str = ""
    raw_stderr: str = ""
    # CLI-reported result subtype (mirrors Claude Code's ``subtype`` field —
    # ``"success"``, ``"error_max_turns"``, ``"error_during_execution"``,
    # ``"error_max_tokens"``, etc.). Populated by adapters that parse
    # structured CLI JSON output. ``None`` for adapters that don't surface a
    # subtype (e.g. genuine subprocess failures with no parsable stdout).
    # Used by the tournament retry layer to short-circuit deterministic
    # failures (see :data:`tournament.llm._DETERMINISTIC_SUBTYPES`).
    # v0.28.0 (Bug 1): adapters may also synthesize a subtype from
    # ``api_error_status`` when the CLI omits its own ``subtype`` on
    # transport-layer failures (401/403 → ``auth_failed``, 429 →
    # ``rate_limited``, 5xx → ``server_error``, other 4xx → ``client_error``).
    subtype: str | None = None
    # v0.28.0 (Bug 1): raw HTTP status code reported by the CLI on
    # transport-layer failures (e.g. 403 from a corp proxy auth-token
    # expiry). Surfaced as a typed integer so downstream ledger logging
    # (Bug 4 in v0.30.0) and post-mortems don't have to grep free-text
    # ``error`` strings or ``.autodev/debug/*.txt`` dumps. ``None`` when
    # the CLI did not report ``api_error_status`` (success path or
    # non-HTTP failure modes).
    api_error_status: int | None = None


class AgentSpec(BaseModel):
    """Definition of an agent (used by `init_workspace` in Phase 3)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    prompt: str
    tools: list[str] = Field(default_factory=list)
    model: str | None = None
    max_turns: int | None = None


class StreamEvent(BaseModel):
    """Reserved for future stream-json parsing; unused in Phase 2."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["tool_start", "tool_end", "text", "error"]
    data: dict[str, Any] = Field(default_factory=dict)
