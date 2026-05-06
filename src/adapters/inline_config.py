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
## autodev

autodev is an external multi-agent orchestrator installed in this project.
The orchestrator drives planning, critique, implementation, review, tests, and
tournament-based self-refinement. Claude Code is one of the executors.

### Kickoff rule (passive)

When the user describes a feature, bug fix, or refactor to build, do NOT
implement it directly. First run:

    autodev plan "<their intent>"

This drafts a phased plan, runs a critic tournament, and writes a delegation
file under `.autodev/delegations/`. Then follow the resume rule below.

The user can also invoke `/autodev <intent>` explicitly — same flow.

### Resume rule (post-delegation)

When a file appears at `.autodev/delegations/`, read it and execute the task
described. After writing your response JSON to the `response_path` specified in
the delegation file's YAML frontmatter, run:

    autodev resume

**Delegation directory:** `.autodev/delegations/`
**Response directory:** `.autodev/responses/`
{_CLAUDE_SECTION_END}
"""


def render_claude_slash_command() -> str:
    """Return the full content for .claude/commands/autodev.md.

    The slash command lets users explicitly drive a feature through
    AutoDev via `/autodev <intent>` (one-shot) or `/autodev --review
    <intent>` (checkpointed).
    """
    return """\
---
description: Plan and ship a feature through the AutoDev multi-agent pipeline.
allowed-tools: [Bash]
argument-hint: [--review] <feature description>
---

The user invoked `/autodev` to drive a feature through AutoDev instead of
implementing directly. Do NOT write code yourself — delegate via autodev.

Args: $ARGUMENTS

Steps:

1. Parse $ARGUMENTS. If it starts with `--review` (or contains `--review` as a
   leading flag), strip the flag and treat the remainder as the intent; set
   review_mode = true. Otherwise review_mode = false.

2. If `.autodev/` does not exist, run `autodev init --inline --force` first.

3. Run `autodev plan "<intent>"` via Bash and surface the plan summary to the
   user (phases, tasks, projected calls).

4. Branch on review_mode:
   - review_mode = false (default, one-shot): immediately run `autodev execute`
     (or, in inline mode, follow the resume rule in CLAUDE.md when delegation
     files appear). Surface progress and final status.
   - review_mode = true: STOP after the plan summary. Tell the user to reply
     with "go" (or equivalent) to proceed; only then run `autodev execute`.

5. If any step fails, surface stderr from autodev verbatim. Do NOT retry
   blindly. Suggest `autodev status` / `autodev doctor` for diagnostics.
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
    "render_claude_slash_command",
    "render_cursor_resume_config",
]
