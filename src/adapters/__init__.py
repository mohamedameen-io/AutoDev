"""Platform adapters for Claude Code and Cursor subscriptions."""

from __future__ import annotations

from adapters.base import PlatformAdapter
from adapters.claude_code import ClaudeCodeAdapter
from adapters.cursor import CursorAdapter
from adapters.detect import PlatformName, detect_platform, get_adapter
from adapters.inline import InlineAdapter
from adapters.types import (
    AgentInvocation,
    AgentResult,
    AgentSpec,
    StreamEvent,
    ToolCall,
)

__all__ = [
    "AgentInvocation",
    "AgentResult",
    "AgentSpec",
    "ClaudeCodeAdapter",
    "CursorAdapter",
    "InlineAdapter",
    "PlatformAdapter",
    "PlatformName",
    "StreamEvent",
    "ToolCall",
    "detect_platform",
    "get_adapter",
]
