"""Tests for :mod:`src.agents.tool_map`."""

from __future__ import annotations

from agents.tool_map import (
    AGENT_TOOL_MAP,
    CLAUDE_CODE_TOOLS,
    resolve_claude_tools,
)
from config.schema import REQUIRED_AGENT_ROLES


def test_all_required_roles_have_mapping() -> None:
    """Every required role must be present in AGENT_TOOL_MAP."""
    missing = [r for r in REQUIRED_AGENT_ROLES if r not in AGENT_TOOL_MAP]
    assert missing == [], f"roles missing from AGENT_TOOL_MAP: {missing}"


def test_all_canonical_names_map_to_claude_tools() -> None:
    """Every canonical tool name in AGENT_TOOL_MAP resolves to a Claude tool."""
    for role, canonical_list in AGENT_TOOL_MAP.items():
        for canonical in canonical_list:
            assert canonical in CLAUDE_CODE_TOOLS, (
                f"role {role} references unknown canonical tool {canonical!r}"
            )


def test_resolve_claude_tools_coder() -> None:
    """Coder must resolve to the full read/write/bash/search Claude toolset."""
    assert resolve_claude_tools("developer") == [
        "Read",
        "Edit",
        "Write",
        "Bash",
        "Glob",
        "Grep",
    ]


def test_resolve_claude_tools_explorer() -> None:
    """Explorer is read-only."""
    assert resolve_claude_tools("explorer") == ["Read", "Glob", "Grep"]


def test_resolve_claude_tools_architect_includes_task() -> None:
    """Architect includes Task for subagent delegation."""
    tools = resolve_claude_tools("architect")
    assert "Task" in tools
    assert "WebSearch" in tools


def test_tournament_roles_empty_tools() -> None:
    """Tournament roles are text-only and must have no tools."""
    for role in ("critic_t", "architect_b", "synthesizer", "judge"):
        assert resolve_claude_tools(role) == [], (
            f"tournament role {role} should have no tools"
        )


def test_resolve_claude_tools_unknown_role() -> None:
    """Unknown role returns empty list (safe default)."""
    assert resolve_claude_tools("nonexistent_role_xyz") == []
