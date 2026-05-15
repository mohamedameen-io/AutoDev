"""Single source of truth for the ``/autodev`` slash-command template.

v0.30.2 hot-fixed a drift bug where ``requeue`` and ``rewind`` were missing
from the Claude template's subcommand list (the Cursor template had stayed
current). The renderers in :mod:`adapters.inline_config` were two
independent string-builder functions, so any change to one had to be
manually mirrored to the other.

This module eliminates that drift class. Both renderers now consume a
single :class:`SlashCommandSpec` and only contribute platform-specific
wrapping (Claude needs YAML frontmatter and ``--platform claude_code``;
Cursor wants no frontmatter and ``--platform cursor``).

The canonical subcommand tuple is derived at module-import time from the
actual CLI registry in :mod:`cli.commands`. A test in
``tests/test_inline_config.py`` re-asserts that equality at every test
run so this file itself cannot drift.
"""

from __future__ import annotations

from dataclasses import dataclass


def _derive_subcommands_from_registry() -> tuple[str, ...]:
    """Introspect the Click group built by :func:`cli.commands.register_commands`.

    Importing ``cli.commands`` is safe at module-import time: the package
    only registers Click commands (no side effects beyond decoration). If
    the import ever becomes awkward (e.g., circular), the
    ``test_canonical_spec_subcommands_match_cli_registry`` lock will catch
    a hardcoded fallback that drifts from the registry.
    """
    import click

    from cli.commands import register_commands

    group = click.Group()
    register_commands(group)
    return tuple(sorted(group.commands.keys()))


_CANONICAL_SUBCOMMANDS: tuple[str, ...] = _derive_subcommands_from_registry()

_CANONICAL_ENTRY_POINTS: tuple[str, ...] = (
    "/autodev <feature>",
    "/autodev --review <feature>",
    "/autodev resume",
    "/autodev status",
    "/autodev doctor",
    "/autodev metrics regex-timeouts",
)


@dataclass(frozen=True)
class SlashCommandSpec:
    """Platform-agnostic spec for the ``/autodev`` slash command body.

    Holds every routing rule, every error-handling instruction and every
    entry-point suggestion in one place. Templates fill in
    ``{tool_phrase}`` (e.g., ``Bash`` vs ``the shell``),
    ``{platform_flag}`` (``claude_code`` or ``cursor``) and
    ``{init_extra_args}`` (the extra args appended to ``autodev init
    --force`` for Cursor).
    """

    description: str
    routing_intro: str
    case_no_args: str
    case_subcommand_header: str
    case_subcommand_body_template: str
    case_review_flag_template: str
    case_feature_description_template: str
    error_handling: str
    subcommands: tuple[str, ...]
    entry_points: tuple[str, ...]


def canonical_slash_command_spec() -> SlashCommandSpec:
    """Build the canonical spec used by both Claude and Cursor renderers."""
    description = (
        "Full AutoDev CLI passthrough — any subcommand, or drive a "
        "feature end-to-end."
    )

    routing_intro = (
        "The user invoked `/autodev`. This command is a **complete "
        "passthrough** to\nthe `autodev` CLI binary — every subcommand "
        "reachable from the shell is\nreachable from here. Do NOT write "
        "code yourself — delegate via `autodev`{delegation_suffix}.\n\n"
        "Args: $ARGUMENTS\n\n"
        "## Routing rule\n\n"
        "Inspect the FIRST whitespace-separated token of $ARGUMENTS:"
    )
    # delegation_suffix is empty for Claude (period attaches directly to
    # `autodev`) and "\nby running it through the shell" for Cursor (the
    # period attaches after "shell").

    case_no_args = (
        "1. **No arguments / empty**: run `autodev --help` via {tool_phrase} "
        "and surface the\n"
        "   subcommand list verbatim. Suggest the most useful entry points:\n"
        "   `/autodev <feature>` (one-shot), `/autodev --review <feature>` "
        "(checkpointed),\n"
        "   `/autodev resume`, `/autodev status`, `/autodev doctor`,\n"
        "   `/autodev metrics regex-timeouts`."
    )

    case_subcommand_header = (
        "2. **First token is one of these registered CLI subcommands**:"
    )

    case_subcommand_body_template = (
        "   — OR a help/version flag (`--help`, `-h`, `--version`).\n\n"
        "   → Direct CLI passthrough. Run `autodev $ARGUMENTS` via "
        "{tool_phrase} and surface\n"
        "   stdout/stderr verbatim. Do NOT re-interpret, do NOT auto-chain "
        "into other\n"
        "   subcommands. The user is asking for that exact subcommand."
    )

    case_review_flag_template = (
        "3. **First token is `--review`**: checkpointed feature flow.\n"
        "   a. Strip the `--review` flag; treat the remainder as the feature "
        "intent.\n"
        "   b. If `.autodev/` does not exist, run `autodev init --force"
        "{init_extra_args}`.\n"
        "   c. Run `autodev plan --platform {platform_flag} \"<intent>\"` and "
        "surface the\n"
        "      plan summary (phases, tasks, projected calls).\n"
        "   d. STOP. Tell the user to reply with `go` (or equivalent) to "
        "proceed.\n"
        "   e. On `go`, run `autodev execute --platform {platform_flag}`."
    )

    case_feature_description_template = (
        "4. **Otherwise** (first token is a free-text feature description): "
        "one-shot\n"
        "   feature flow.\n"
        "   a. If `.autodev/` does not exist, run `autodev init --force"
        "{init_extra_args}`.\n"
        "   b. Run `autodev plan --platform {platform_flag} \"$ARGUMENTS\"` "
        "and surface the\n"
        "      plan summary.\n"
        "   c. Run `autodev execute --platform {platform_flag}`. Surface "
        "progress + final\n"
        "      status."
    )

    error_handling = (
        "## Error handling\n\n"
        "If any `autodev` invocation fails (non-zero exit), surface stderr "
        "verbatim.\n"
        "Do NOT retry blindly. Suggest `autodev doctor` and `autodev status` "
        "for\n"
        "diagnostics, and `/autodev resume` after fixing the underlying "
        "issue."
    )

    return SlashCommandSpec(
        description=description,
        routing_intro=routing_intro,
        case_no_args=case_no_args,
        case_subcommand_header=case_subcommand_header,
        case_subcommand_body_template=case_subcommand_body_template,
        case_review_flag_template=case_review_flag_template,
        case_feature_description_template=case_feature_description_template,
        error_handling=error_handling,
        subcommands=_CANONICAL_SUBCOMMANDS,
        entry_points=_CANONICAL_ENTRY_POINTS,
    )


def _format_subcommand_list(subcommands: tuple[str, ...]) -> str:
    """Render the subcommand tuple as the indented, backticked, wrapped list
    used inside case 2 of the routing rule. Mirrors the visual layout of
    the prior hand-written templates: ~3 lines of comma-separated
    backticked names indented by three spaces. Commas appear between every
    pair (including across line breaks); only the final token has no
    trailing comma.
    """
    quoted = [f"`{name}`" for name in subcommands]
    # Build "tok, " segments, then greedily pack into lines of bounded
    # width. The trailing ", " is dropped from the very last segment.
    segments = [f"{tok}, " for tok in quoted[:-1]] + [quoted[-1]]
    lines: list[str] = []
    current = ""
    max_len = 60
    for seg in segments:
        if current and len(current) + len(seg) > max_len:
            lines.append(current.rstrip())
            current = seg
        else:
            current += seg
    if current:
        lines.append(current.rstrip())
    indent = "   "
    return "\n".join(f"{indent}{line}" for line in lines)


def render_slash_command_body(
    spec: SlashCommandSpec,
    *,
    tool_phrase: str,
    platform_flag: str,
    init_extra_args: str,
    delegation_suffix: str = "",
) -> str:
    """Render the platform-agnostic body shared by both Claude and Cursor.

    The caller is responsible for prepending any platform-specific header
    (e.g., Claude Code's YAML frontmatter).
    """
    intro = spec.routing_intro.format(delegation_suffix=delegation_suffix)
    no_args = spec.case_no_args.format(tool_phrase=tool_phrase)
    subcommand_list = _format_subcommand_list(spec.subcommands)
    sub_body = spec.case_subcommand_body_template.format(tool_phrase=tool_phrase)
    review_case = spec.case_review_flag_template.format(
        platform_flag=platform_flag, init_extra_args=init_extra_args
    )
    feature_case = spec.case_feature_description_template.format(
        platform_flag=platform_flag, init_extra_args=init_extra_args
    )
    return (
        f"{intro}\n\n"
        f"{no_args}\n\n"
        f"{spec.case_subcommand_header}\n"
        f"{subcommand_list}\n"
        f"{sub_body}\n\n"
        f"{review_case}\n\n"
        f"{feature_case}\n\n"
        f"{spec.error_handling}\n"
    )


__all__ = [
    "SlashCommandSpec",
    "canonical_slash_command_spec",
    "render_slash_command_body",
]
