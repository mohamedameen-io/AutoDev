"""Render the ``/autodev`` slash-command template.

v0.26.0: InlineAdapter is gone, so this module no longer renders
resume/CLAUDE.md sections that instructed agents to read delegation
files and run ``autodev resume``. What remains is the subprocess-only
slash-command template plus a small helper that preserves any legacy
``<!-- autodev-managed -->`` section in a user's existing ``CLAUDE.md``
when migrating workspaces from <=v0.25.x.

v0.31.0 (Phase 4): the Claude and Cursor templates are now rendered
from a single :class:`adapters.slash_command_spec.SlashCommandSpec`.
The two render functions below are thin platform-wrapping shims —
Claude prepends YAML frontmatter and uses ``--platform claude_code``;
Cursor uses no frontmatter, ``--platform cursor`` and a
``--platform cursor`` extra arg on ``autodev init --force``. Every
routing rule, subcommand list entry, and error-handling instruction
lives in the spec, so the two templates cannot drift again.
"""

from __future__ import annotations

from adapters.slash_command_spec import (
    canonical_slash_command_spec,
    render_slash_command_body,
)

_CLAUDE_SECTION_START = "<!-- autodev-managed: do not edit this section -->"
_CLAUDE_SECTION_END = "<!-- /autodev-managed -->"


def render_claude_slash_command() -> str:
    """Return the full content for ``.claude/commands/autodev.md``.

    v0.24.2: the slash command is a **full CLI passthrough**.
    v0.26.0: every dispatch is subprocess (``--platform claude_code``);
    InlineAdapter is gone, so the slash command never embeds itself in
    the host agent's session — it always shells out to ``autodev``.
    v0.31.0: body is built from
    :func:`adapters.slash_command_spec.canonical_slash_command_spec` so
    it cannot drift from the Cursor variant.

    * ``/autodev`` (no args) prints the subcommand list.
    * ``/autodev <subcommand> [...]`` (where ``<subcommand>`` is any
      registered ``autodev`` CLI subcommand) runs ``autodev <subcommand>
      [...]`` verbatim and surfaces the output. This means
      ``/autodev resume``, ``/autodev status``, ``/autodev metrics
      regex-timeouts``, ``/autodev doctor``, etc. all work.
    * ``/autodev [--review] <feature description>`` (legacy intent flow)
      drives a feature through ``plan`` → ``execute`` end-to-end.
    """
    spec = canonical_slash_command_spec()
    frontmatter = (
        "---\n"
        f"description: {spec.description}\n"
        "allowed-tools: [Bash]\n"
        "argument-hint: <subcommand> [args] | [--review] <feature description>\n"
        "---\n\n"
    )
    body = render_slash_command_body(
        spec,
        tool_phrase="Bash",
        platform_flag="claude_code",
        init_extra_args="",
        delegation_suffix="",
    )
    return frontmatter + body


def render_cursor_slash_command() -> str:
    """Return the full content for ``.cursor/commands/autodev.md``.

    v0.30.1: Cursor 1.6+ supports custom slash commands as plain
    markdown files at ``.cursor/commands/<name>.md``. Unlike Claude
    Code's slash commands, Cursor commands have **no required
    frontmatter** — no ``allowed-tools:``, no ``argument-hint:``. The
    file body is loaded into the Composer agent's input box as a
    reusable prompt template.

    The routing rules mirror :func:`render_claude_slash_command` but
    pass ``--platform cursor`` to every ``autodev`` invocation so
    on-disk configs and per-trigger artifacts stay tagged correctly.

    v0.31.0: body comes from the same
    :class:`adapters.slash_command_spec.SlashCommandSpec` the Claude
    renderer uses; only platform flags and the "via the shell" tool
    phrase differ.

    * ``/autodev`` (no args) prints the subcommand list.
    * ``/autodev <subcommand> [...]`` (where ``<subcommand>`` is any
      registered ``autodev`` CLI subcommand) runs ``autodev <subcommand>
      [...]`` verbatim and surfaces the output.
    * ``/autodev [--review] <feature description>`` (legacy intent flow)
      drives a feature through ``plan`` → ``execute`` end-to-end.
    """
    spec = canonical_slash_command_spec()
    return render_slash_command_body(
        spec,
        tool_phrase="the shell",
        platform_flag="cursor",
        init_extra_args=" --platform cursor",
        delegation_suffix="\nby running it through the shell",
    )


def update_claude_md(content: str, section: str) -> str:
    """Replace or append the autodev-managed section in CLAUDE.md.

    Retained for v0.26.0 so legacy ``<!-- autodev-managed -->`` sections
    written by <=v0.25.x can be cleanly replaced or removed on the next
    ``autodev init --force``. If the section delimiters exist, replace
    the content between them. Otherwise, append the section at the end.
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
    "render_claude_slash_command",
    "render_cursor_slash_command",
    "update_claude_md",
]
