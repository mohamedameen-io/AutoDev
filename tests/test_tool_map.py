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


def test_text_only_tournament_roles_empty_tools() -> None:
    """Pure text-in/text-out tournament roles must have no tools.

    ``architect_b`` is deliberately EXCLUDED here — WS-5 grants it Read + Bash
    so the plan critic can empirically falsify a suspect acceptance oracle
    (see ``test_architect_b_can_falsify_oracle``). The remaining tournament
    roles (critic_t / synthesizer / judge) stay text-only.
    """
    for role in ("critic_t", "synthesizer", "judge"):
        assert resolve_claude_tools(role) == [], (
            f"tournament role {role} should have no tools"
        )


def test_architect_b_can_falsify_oracle() -> None:
    """WS-5: the plan critic ``architect_b`` must resolve to Read + Bash so it
    can execute a reproduction and check a bug-fix acceptance oracle
    empirically — it can no longer be silently limited to Read-only.

    The grant is deliberately scoped to read-only reconnaissance + execution
    (Read/Glob/Grep/Bash). A critic must NOT mutate the tree, so Edit/Write are
    withheld.
    """
    tools = resolve_claude_tools("architect_b")
    assert "Read" in tools, "architect_b needs Read to inspect the repro/tests"
    assert "Bash" in tools, (
        "architect_b needs Bash to execute a reproduction and falsify the oracle"
    )
    # Scoped: a critic reproduces + inspects, it does not implement.
    assert "Edit" not in tools and "Write" not in tools, (
        "architect_b is a critic — it must not carry Edit/Write (no tree mutation)"
    )
    # The grant is non-empty, so the empty->['Read'] sentinel path in
    # AdapterLLMClient no longer applies to architect_b.
    assert tools != ["Read"], (
        "architect_b resolving to bare ['Read'] means the Bash grant did not flow"
    )


def test_resolve_claude_tools_unknown_role() -> None:
    """Unknown role returns empty list (safe default)."""
    assert resolve_claude_tools("nonexistent_role_xyz") == []
