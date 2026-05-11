"""Tests for the ``/autodev`` slash-command renderer and the legacy
``update_claude_md`` helper.

v0.26.0: InlineAdapter and its ``init_workspace`` / resume-config
renderers were removed. What survives is the subprocess-only
``render_claude_slash_command()`` template and the
``update_claude_md`` helper that handles the migration-path case
where a pre-v0.26.0 workspace already has an
``<!-- autodev-managed -->`` section in its CLAUDE.md.
"""

from __future__ import annotations


from adapters.inline_config import (
    _CLAUDE_SECTION_END,
    _CLAUDE_SECTION_START,
    render_claude_slash_command,
    update_claude_md,
)


# ---------------------------------------------------------------------------
# update_claude_md (migration helper)
# ---------------------------------------------------------------------------


def test_update_claude_md_replaces_existing_section() -> None:
    old_section = f"{_CLAUDE_SECTION_START}\nold content\n{_CLAUDE_SECTION_END}"
    new_section = (
        f"{_CLAUDE_SECTION_START}\nfresh content\n{_CLAUDE_SECTION_END}"
    )
    result = update_claude_md(old_section, new_section)
    assert "old content" not in result
    assert "fresh content" in result
    assert _CLAUDE_SECTION_START in result
    assert _CLAUDE_SECTION_END in result


def test_update_claude_md_appends_when_no_delimiters() -> None:
    existing = "# My Project\n\nSome user content.\n"
    section = f"{_CLAUDE_SECTION_START}\nbody\n{_CLAUDE_SECTION_END}"
    result = update_claude_md(existing, section)
    assert result.startswith("# My Project")
    assert _CLAUDE_SECTION_START in result


def test_update_claude_md_preserves_surrounding_user_content() -> None:
    before = "# Header\n\nUser content before.\n\n"
    after = "\n\nUser content after.\n"
    old_section = f"{_CLAUDE_SECTION_START}\nold\n{_CLAUDE_SECTION_END}"
    content = before + old_section + after
    new_section = f"{_CLAUDE_SECTION_START}\nnew\n{_CLAUDE_SECTION_END}"
    result = update_claude_md(content, new_section)
    assert result.startswith("# Header")
    assert "User content before." in result
    assert "User content after." in result
    assert "old" not in result
    assert "new" in result


def test_update_claude_md_handles_empty_content() -> None:
    section = f"{_CLAUDE_SECTION_START}\nbody\n{_CLAUDE_SECTION_END}"
    result = update_claude_md("", section)
    assert result == section


# ---------------------------------------------------------------------------
# render_claude_slash_command (subprocess-only CLI passthrough)
# ---------------------------------------------------------------------------


def test_render_claude_slash_command_has_frontmatter() -> None:
    result = render_claude_slash_command()
    assert result.startswith("---\n")
    assert "description:" in result
    assert "allowed-tools:" in result
    assert "argument-hint:" in result


def test_render_claude_slash_command_documents_review_flag() -> None:
    result = render_claude_slash_command()
    assert "--review" in result
    assert "$ARGUMENTS" in result
    assert "autodev plan" in result
    assert "autodev execute" in result


def test_render_claude_slash_command_lists_every_cli_subcommand() -> None:
    """Each registered CLI subcommand MUST appear in the routing rule list,
    so the template stays in lock-step with src/cli/commands/."""
    result = render_claude_slash_command()
    for sub in (
        "doctor",
        "execute",
        "init",
        "logs",
        "metrics",
        "plan",
        "plugins",
        "prune",
        "reset",
        "resume",
        "secretscan",
        "status",
        "tournament",
    ):
        assert f"`{sub}`" in result, f"missing subcommand {sub!r} in slash template"


def test_render_claude_slash_command_documents_passthrough_routing() -> None:
    """The template must explain the routing rule (subcommand vs intent)."""
    result = render_claude_slash_command()
    assert "passthrough" in result.lower()
    # The template should describe both flows: direct subcommand AND intent.
    assert "feature description" in result.lower() or "intent" in result.lower()
    # Help/version flags must be reachable without falling into intent flow.
    assert "--help" in result
    assert "--version" in result


def test_render_claude_slash_command_documents_resume_status_doctor() -> None:
    """The most common direct invocations must be discoverable in the template."""
    result = render_claude_slash_command()
    assert "/autodev resume" in result
    assert "/autodev status" in result
    assert "/autodev doctor" in result


def test_render_claude_slash_command_passes_platform_claude_code() -> None:
    """v0.26.0: the template must pass ``--platform claude_code`` to the
    ``plan`` and ``execute`` subprocess invocations so legacy on-disk
    configs with ``platform: inline`` cannot leak through (the schema
    migrator catches it too; this is belt-and-suspenders at the surface).
    """
    result = render_claude_slash_command()
    assert "autodev plan --platform claude_code" in result
    assert "autodev execute --platform claude_code" in result


def test_render_claude_slash_command_does_not_reference_inline_flag() -> None:
    """v0.26.0: the slash command must not reference the deprecated
    ``--inline`` flag or any inline-mode resume rule."""
    result = render_claude_slash_command()
    assert "--inline" not in result
    # The ``in inline mode, follow the resume rule in CLAUDE.md`` clause
    # from v0.25.x is gone.
    assert "in inline mode" not in result.lower()
