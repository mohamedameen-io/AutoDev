"""Tests for inline_config renderers and InlineAdapter.init_workspace()."""

from __future__ import annotations

import asyncio
from pathlib import Path


from adapters.inline import InlineAdapter
from adapters.inline_config import (
    _CLAUDE_SECTION_END,
    _CLAUDE_SECTION_START,
    render_claude_resume_config,
    render_cursor_resume_config,
    update_claude_md,
)
from adapters.types import AgentSpec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_specs() -> list[AgentSpec]:
    return [
        AgentSpec(name="developer", description="writes code", prompt="You are a coder.")
    ]


# ---------------------------------------------------------------------------
# 1. render_claude_resume_config produces valid Markdown with section delimiters
# ---------------------------------------------------------------------------


def test_render_claude_resume_config_has_delimiters() -> None:
    result = render_claude_resume_config()
    assert _CLAUDE_SECTION_START in result
    assert _CLAUDE_SECTION_END in result
    assert result.startswith(_CLAUDE_SECTION_START)


# ---------------------------------------------------------------------------
# 2. render_cursor_resume_config produces valid mdc with frontmatter
# ---------------------------------------------------------------------------


def test_render_cursor_resume_config_has_frontmatter() -> None:
    result = render_cursor_resume_config()
    assert result.startswith("---\n")
    assert "alwaysApply: true" in result
    assert "description:" in result
    assert "autodev resume" in result


# ---------------------------------------------------------------------------
# 3. update_claude_md replaces existing section
# ---------------------------------------------------------------------------


def test_update_claude_md_replaces_existing_section() -> None:
    old_section = f"{_CLAUDE_SECTION_START}\nold content\n{_CLAUDE_SECTION_END}"
    new_section = render_claude_resume_config()
    result = update_claude_md(old_section, new_section)
    assert "old content" not in result
    assert _CLAUDE_SECTION_START in result
    assert _CLAUDE_SECTION_END in result
    assert "autodev resume" in result


# ---------------------------------------------------------------------------
# 4. update_claude_md appends section when delimiters not found
# ---------------------------------------------------------------------------


def test_update_claude_md_appends_when_no_delimiters() -> None:
    existing = "# My Project\n\nSome user content.\n"
    section = render_claude_resume_config()
    result = update_claude_md(existing, section)
    assert result.startswith("# My Project")
    assert _CLAUDE_SECTION_START in result
    assert "autodev resume" in result


# ---------------------------------------------------------------------------
# 5. update_claude_md preserves user content before/after section
# ---------------------------------------------------------------------------


def test_update_claude_md_preserves_surrounding_user_content() -> None:
    before = "# Header\n\nUser content before.\n\n"
    after = "\n\nUser content after.\n"
    old_section = f"{_CLAUDE_SECTION_START}\nold\n{_CLAUDE_SECTION_END}"
    content = before + old_section + after
    new_section = render_claude_resume_config()
    result = update_claude_md(content, new_section)
    assert result.startswith("# Header")
    assert "User content before." in result
    assert "User content after." in result
    assert "old" not in result
    assert "autodev resume" in result


# ---------------------------------------------------------------------------
# 6. update_claude_md handles empty content
# ---------------------------------------------------------------------------


def test_update_claude_md_handles_empty_content() -> None:
    section = render_claude_resume_config()
    result = update_claude_md("", section)
    assert result == section


# ---------------------------------------------------------------------------
# 7. InlineAdapter.init_workspace creates directories for claude_code hint
# ---------------------------------------------------------------------------


def test_init_workspace_creates_dirs_for_claude_code(tmp_path: Path) -> None:
    adapter = InlineAdapter(cwd=tmp_path, platform_hint="claude_code")
    asyncio.run(adapter.init_workspace(tmp_path, _make_specs()))

    assert (tmp_path / ".autodev" / "delegations").is_dir()
    assert (tmp_path / ".autodev" / "responses").is_dir()
    assert (tmp_path / ".claude" / "agents").is_dir()
    assert (tmp_path / ".claude" / "CLAUDE.md").is_file()


# ---------------------------------------------------------------------------
# 8. InlineAdapter.init_workspace creates directories for cursor hint
# ---------------------------------------------------------------------------


def test_init_workspace_creates_dirs_for_cursor(tmp_path: Path) -> None:
    adapter = InlineAdapter(cwd=tmp_path, platform_hint="cursor")
    asyncio.run(adapter.init_workspace(tmp_path, _make_specs()))

    assert (tmp_path / ".autodev" / "delegations").is_dir()
    assert (tmp_path / ".autodev" / "responses").is_dir()
    assert (tmp_path / ".cursor" / "rules").is_dir()
    assert (tmp_path / ".cursor" / "rules" / "src.mdc").is_file()


# ---------------------------------------------------------------------------
# 9. InlineAdapter.init_workspace is idempotent (calling twice same result)
# ---------------------------------------------------------------------------


def test_init_workspace_is_idempotent(tmp_path: Path) -> None:
    adapter = InlineAdapter(cwd=tmp_path, platform_hint="claude_code")
    asyncio.run(adapter.init_workspace(tmp_path, _make_specs()))
    first_content = (tmp_path / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")

    asyncio.run(adapter.init_workspace(tmp_path, _make_specs()))
    second_content = (tmp_path / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")

    assert first_content == second_content


# ---------------------------------------------------------------------------
# 10. InlineAdapter.init_workspace preserves existing CLAUDE.md user content
# ---------------------------------------------------------------------------


def test_init_workspace_preserves_existing_claude_md_user_content(
    tmp_path: Path,
) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    user_content = "# My Project\n\nThis is my custom CLAUDE.md content.\n"
    (claude_dir / "CLAUDE.md").write_text(user_content, encoding="utf-8")

    adapter = InlineAdapter(cwd=tmp_path, platform_hint="claude_code")
    asyncio.run(adapter.init_workspace(tmp_path, _make_specs()))

    result = (claude_dir / "CLAUDE.md").read_text(encoding="utf-8")
    assert "# My Project" in result
    assert "This is my custom CLAUDE.md content." in result
    assert _CLAUDE_SECTION_START in result
    assert "autodev resume" in result
