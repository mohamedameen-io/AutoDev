"""Tool allow-list for each agent role.

Opencode-swarm's tools (e.g. `checkpoint`, `check_gate_status`, `diff`) are
plugin-specific and do not have direct Claude Code / Cursor equivalents. The
canonical list below names the minimum Claude Code tools each role needs to
perform its function; `AGENT_TOOL_MAP` maps role -> canonical list, and
`resolve_claude_tools` translates canonical -> Claude Code tool names.

Tournament roles (`critic_t`, `author_b`, `synthesizer`, `judge`) are text-only
and require no tools — they receive rendered proposals and return text.
"""

from __future__ import annotations


# Claude Code's built-in tool names (capitalized).
CLAUDE_CODE_TOOLS: dict[str, str] = {
    "read": "Read",
    "edit": "Edit",
    "write": "Write",
    "bash": "Bash",
    "glob": "Glob",
    "grep": "Grep",
    "web_search": "WebSearch",
    "web_fetch": "WebFetch",
    "task": "Task",
    "notebook_edit": "NotebookEdit",
}


# Canonical tool names per role. Opencode-specific fine-grained tools
# (e.g., `symbols`, `imports`, `diff_summary`) collapse to `read` / `grep` /
# `bash` — the base capabilities those helpers ultimately wrap.
AGENT_TOOL_MAP: dict[str, list[str]] = {
    # Architect: plan drafting, delegation decisions; reasoning-heavy, needs
    # exploration + delegation (Task for subagent invocation when available).
    "architect": ["read", "glob", "grep", "web_search", "web_fetch", "task"],
    # Explorer: read-only codebase reconnaissance.
    "explorer": ["read", "glob", "grep"],
    # Domain expert: domain research; read-only with web access for documentation.
    "domain_expert": ["read", "glob", "grep", "web_search", "web_fetch"],
    # Developer: writes code; full read/write/bash.
    "developer": ["read", "edit", "write", "bash", "glob", "grep"],
    # Reviewer: read-only verification.
    "reviewer": ["read", "glob", "grep"],
    # Test engineer: writes tests and runs them.
    "test_engineer": ["read", "edit", "write", "bash", "glob", "grep"],
    # Sounding board: read-only pushback.
    "critic_sounding_board": ["read", "glob", "grep"],
    # Drift verifier: read-only phase verification.
    "critic_drift_verifier": ["read", "glob", "grep"],
    # Docs: writes to documentation files.
    "docs": ["read", "edit", "write", "glob", "grep"],
    # Designer: read-only design spec generation with web access.
    "designer": ["read", "glob", "grep", "web_fetch"],
    # Tournament revision author / plan critic. WS-5: granted read-only
    # reconnaissance + execution so it can run a reproduction and check a
    # bug-fix acceptance oracle EMPIRICALLY — a buggy issue-example must not
    # become an un-refutable oracle just because the critic was Read-only /
    # Bash-denied. Scoped like `reviewer` (read/glob/grep) PLUS `bash`: it may
    # reproduce + inspect, but NOT mutate the tree (no edit/write) and NOT
    # delegate/browse (no task/web_*). Blast radius: adds bash-capable turns to
    # the plan phase — see prompts.py ARCHITECT_B_PROMPT for the turn-budget
    # guard that keeps reproductions cheap.
    "architect_b": ["read", "glob", "grep", "bash"],
    # Tournament roles: pure text-in/text-out. No tools.
    "critic_t": [],
    "synthesizer": [],
    "judge": [],
}


def resolve_claude_tools(role: str) -> list[str]:
    """Return Claude Code tool names for a role.

    Returns ``[]`` if the role has no canonical tools (e.g., tournament roles)
    or if the role is unknown.

    >>> resolve_claude_tools("developer")
    ['Read', 'Edit', 'Write', 'Bash', 'Glob', 'Grep']
    >>> resolve_claude_tools("critic_t")
    []
    """
    canonical = AGENT_TOOL_MAP.get(role, [])
    return [CLAUDE_CODE_TOOLS[c] for c in canonical if c in CLAUDE_CODE_TOOLS]
