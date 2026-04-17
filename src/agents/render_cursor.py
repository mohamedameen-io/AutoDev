"""Render :class:`AgentSpec` entries to ``.cursor/rules/<name>.mdc`` files.

Cursor's MDC format uses a YAML frontmatter with ``description``, an optional
``globs`` list, and an ``alwaysApply`` boolean. Since autodev invokes Cursor
agents via explicit prompts (not contextually), we set ``alwaysApply: false``
so the rule is opt-in.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from adapters.types import AgentSpec


def _build_frontmatter(spec: AgentSpec) -> str:
    data: dict[str, object] = {
        "description": spec.description,
        "alwaysApply": False,
    }
    yaml_body = yaml.safe_dump(
        data, sort_keys=False, default_flow_style=False, allow_unicode=True
    )
    return f"---\n{yaml_body}---\n"


def _render_one(spec: AgentSpec) -> str:
    fm = _build_frontmatter(spec)
    body = spec.prompt.rstrip()
    return f"{fm}\n# {spec.name} role\n\n{body}\n"


def render_cursor_rules(
    specs: dict[str, AgentSpec], target_dir: Path
) -> list[Path]:
    """Write all agent specs to ``target_dir/.cursor/rules/<name>.mdc``.

    Creates ``target_dir/.cursor/rules/`` if needed. Returns the list of
    written paths in deterministic (sorted) order.
    """
    out_dir = Path(target_dir) / ".cursor" / "rules"
    out_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for name in sorted(specs):
        spec = specs[name]
        path = out_dir / f"{spec.name}.mdc"
        path.write_text(_render_one(spec))
        written.append(path)
    return written


__all__ = ["render_cursor_rules"]
