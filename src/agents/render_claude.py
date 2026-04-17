"""Render :class:`AgentSpec` entries to ``.claude/agents/<name>.md`` files.

Claude Code expects a YAML frontmatter block with ``name``, ``description``,
``tools`` (list), and ``model`` (string). The rest of the file is the prompt
body.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from adapters.types import AgentSpec


def _build_frontmatter(spec: AgentSpec) -> str:
    """Return the YAML frontmatter block (with surrounding ``---`` markers)."""
    data: dict[str, object] = {
        "name": spec.name,
        "description": spec.description,
        "tools": list(spec.tools),
    }
    if spec.model is not None:
        data["model"] = spec.model
    yaml_body = yaml.safe_dump(
        data, sort_keys=False, default_flow_style=False, allow_unicode=True
    )
    return f"---\n{yaml_body}---\n"


def _render_one(spec: AgentSpec) -> str:
    """Return the full ``.md`` content (frontmatter + body)."""
    return _build_frontmatter(spec) + "\n" + spec.prompt.rstrip() + "\n"


def render_claude_agents(
    specs: dict[str, AgentSpec], target_dir: Path
) -> list[Path]:
    """Write all agent specs to ``target_dir/.claude/agents/<name>.md``.

    Creates ``target_dir/.claude/agents/`` if needed. Returns the list of
    written paths in deterministic (sorted) order.
    """
    out_dir = Path(target_dir) / ".claude" / "agents"
    out_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for name in sorted(specs):
        spec = specs[name]
        path = out_dir / f"{spec.name}.md"
        path.write_text(_render_one(spec))
        written.append(path)
    return written


__all__ = ["render_claude_agents"]
