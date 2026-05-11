"""Render the ``/autodev`` slash-command template.

v0.26.0: InlineAdapter is gone, so this module no longer renders
resume/CLAUDE.md sections that instructed agents to read delegation
files and run ``autodev resume``. What remains is the subprocess-only
slash-command template plus a small helper that preserves any legacy
``<!-- autodev-managed -->`` section in a user's existing ``CLAUDE.md``
when migrating workspaces from <=v0.25.x.
"""

from __future__ import annotations

_CLAUDE_SECTION_START = "<!-- autodev-managed: do not edit this section -->"
_CLAUDE_SECTION_END = "<!-- /autodev-managed -->"


def render_claude_slash_command() -> str:
    """Return the full content for ``.claude/commands/autodev.md``.

    v0.24.2: the slash command is a **full CLI passthrough**.
    v0.26.0: every dispatch is subprocess (``--platform claude_code``);
    InlineAdapter is gone, so the slash command never embeds itself in
    the host agent's session — it always shells out to ``autodev``.

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
   b. If `.autodev/` does not exist, run `autodev init --force`
      (defaults to platform: claude_code in v0.26.0).
   c. Run `autodev plan --platform claude_code "<intent>"` and surface the
      plan summary (phases, tasks, projected calls).
   d. STOP. Tell the user to reply with `go` (or equivalent) to proceed.
   e. On `go`, run `autodev execute --platform claude_code`.

4. **Otherwise** (first token is a free-text feature description): one-shot
   feature flow.
   a. If `.autodev/` does not exist, run `autodev init --force`
      (defaults to platform: claude_code in v0.26.0).
   b. Run `autodev plan --platform claude_code "$ARGUMENTS"` and surface the
      plan summary.
   c. Run `autodev execute --platform claude_code`. Surface progress + final
      status.

## Error handling

If any `autodev` invocation fails (non-zero exit), surface stderr verbatim.
Do NOT retry blindly. Suggest `autodev doctor` and `autodev status` for
diagnostics, and `/autodev resume` after fixing the underlying issue.
"""


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
    "update_claude_md",
]
