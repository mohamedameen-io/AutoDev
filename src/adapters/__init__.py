"""Platform adapters for Claude Code and Cursor subscriptions.

v0.26.0: ``InlineAdapter`` was removed. Every dispatch now goes through
a subprocess adapter (``ClaudeCodeAdapter`` or ``CursorAdapter``); the
file-based delegation/response state machine that backed inline mode
in <=v0.25.x is gone.
"""

from __future__ import annotations

from adapters.base import PlatformAdapter
from adapters.claude_code import ClaudeCodeAdapter
from adapters.cursor import CursorAdapter
from adapters.detect import PlatformName, detect_platform, get_adapter
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
    "PlatformAdapter",
    "PlatformName",
    "StreamEvent",
    "ToolCall",
    "detect_platform",
    "get_adapter",
]
