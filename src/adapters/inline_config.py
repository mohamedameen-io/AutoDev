"""Render auto-resume config files for Claude Code and Cursor.

These files instruct the agent to read delegation files from
``.autodev/delegations/``, execute them, write responses to
``.autodev/responses/``, and run ``autodev resume``.
"""

from __future__ import annotations

_CLAUDE_SECTION_START = "<!-- autodev-managed: do not edit this section -->"
_CLAUDE_SECTION_END = "<!-- /autodev-managed -->"


def render_claude_resume_config() -> str:
    """Return the autodev-managed section for .claude/CLAUDE.md.

    The section is delimited by HTML comments so init_workspace() can
    idempotently update it without clobbering user content.
    """
    return f"""\
{_CLAUDE_SECTION_START}
## autodev Auto-Resume

autodev is running in inline mode. When a file appears at
``.autodev/delegations/``, read it and execute the task described.
After writing your response JSON to the ``response_path`` specified
in the delegation file's YAML frontmatter, run:

```bash
autodev resume
```

This continues the autodev orchestration loop.

**Delegation directory:** ``.autodev/delegations/``
**Response directory:** ``.autodev/responses/``
{_CLAUDE_SECTION_END}
"""


def render_cursor_resume_config() -> str:
    """Return the full content for .cursor/rules/src.mdc."""
    return """\
---
description: autodev inline orchestration rules
alwaysApply: true
---

# autodev Inline Mode

autodev is running in inline mode. When a file appears at
`.autodev/delegations/`, read it and execute the task described.

After writing your response JSON to the `response_path` specified
in the delegation file's YAML frontmatter, run:

```bash
autodev resume
```

Response JSON schema:
- `schema_version`: "1.0"
- `task_id`: string (copy from delegation)
- `role`: string (copy from delegation)
- `success`: boolean
- `text`: your prose response
- `error`: null or error string
- `duration_s`: float
- `files_changed`: list of relative paths
- `diff`: unified diff string or null
"""


def update_claude_md(content: str, section: str) -> str:
    """Replace or append the autodev-managed section in CLAUDE.md.

    If the section delimiters exist, replace the content between them.
    Otherwise, append the section at the end.
    """
    start_idx = content.find(_CLAUDE_SECTION_START)
    end_idx = content.find(_CLAUDE_SECTION_END)
    if start_idx >= 0 and end_idx >= 0:
        end_idx += len(_CLAUDE_SECTION_END)
        # Preserve any user content after the section, but strip the
        # trailing newline that was part of the previous section render.
        trailing = content[end_idx:]
        if trailing == "\n":
            trailing = ""
        return content[:start_idx] + section + trailing
    # Append with a leading newline only if content doesn't already end with one.
    if content and not content.endswith("\n"):
        return content + "\n" + section
    return content + section if content else section


__all__ = [
    "update_claude_md",
    "render_claude_resume_config",
    "render_cursor_resume_config",
]
