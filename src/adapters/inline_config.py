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
    """Return the full content for ``.claude/commands/autodev.md``.

    v0.24.2: the slash command is now a **full CLI passthrough**.

    * ``/autodev`` (no args) prints the subcommand list.
    * ``/autodev <subcommand> [...]`` (where ``<subcommand>`` is any
      registered ``autodev`` CLI subcommand) runs ``autodev <subcommand>
      [...]`` verbatim and surfaces the output. This means
      ``/autodev resume``, ``/autodev status``, ``/autodev metrics
      regex-timeouts``, ``/autodev doctor``, etc. all work.
    * ``/autodev [--review] <feature description>`` (legacy intent flow)
      drives a feature through ``plan`` → ``execute`` end-to-end.
    """
    return """\
---
description: Full AutoDev CLI passthrough — any subcommand, or drive a feature end-to-end.
allowed-tools: [Bash]
argument-hint: <subcommand> [args] | [--review] <feature description>
---

The user invoked `/autodev`. This command is a **complete passthrough** to
the `autodev` CLI binary — every subcommand reachable from the shell is
reachable from here. Do NOT write code yourself — delegate via `autodev`.

Args: $ARGUMENTS

## Routing rule

Inspect the FIRST whitespace-separated token of $ARGUMENTS:

1. **No arguments / empty**: run `autodev --help` via Bash and surface the
   subcommand list verbatim. Suggest the most useful entry points:
   `/autodev <feature>` (one-shot), `/autodev --review <feature>` (checkpointed),
   `/autodev resume`, `/autodev status`, `/autodev doctor`,
   `/autodev metrics regex-timeouts`.

2. **First token is one of these registered CLI subcommands**:
   `doctor`, `execute`, `init`, `logs`, `metrics`, `plan`, `plugins`,
   `prune`, `reset`, `resume`, `secretscan`, `status`, `tournament`
   — OR a help/version flag (`--help`, `-h`, `--version`).

   → Direct CLI passthrough. Run `autodev $ARGUMENTS` via Bash and surface
   stdout/stderr verbatim. Do NOT re-interpret, do NOT auto-chain into other
   subcommands. The user is asking for that exact subcommand.

3. **First token is `--review`**: checkpointed feature flow.
   a. Strip the `--review` flag; treat the remainder as the feature intent.
   b. If `.autodev/` does not exist, run `autodev init --inline --force`.
   c. Run `autodev plan "<intent>"` and surface the plan summary (phases,
      tasks, projected calls).
   d. STOP. Tell the user to reply with `go` (or equivalent) to proceed.
   e. On `go`, run `autodev execute`.

4. **Otherwise** (first token is a free-text feature description): one-shot
   feature flow.
   a. If `.autodev/` does not exist, run `autodev init --inline --force`.
   b. Run `autodev plan "$ARGUMENTS"` and surface the plan summary.
   c. Run `autodev execute` (or, in inline mode, follow the resume rule in
      CLAUDE.md when delegation files appear). Surface progress + final
      status.

## Error handling

If any `autodev` invocation fails (non-zero exit), surface stderr verbatim.
Do NOT retry blindly. Suggest `autodev doctor` and `autodev status` for
diagnostics, and `/autodev resume` after fixing the underlying issue.
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
