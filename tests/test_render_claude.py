"""Tests for :mod:`src.agents.render_claude`."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from agents import build_registry
from agents.render_claude import render_claude_agents
from config.defaults import default_config


GOLDEN_DIR = Path(__file__).parent / "golden" / "claude_agents"


@pytest.fixture
def rendered_dir(tmp_path: Path) -> Path:
    specs = build_registry(default_config())
    render_claude_agents(specs, tmp_path)
    return tmp_path / ".claude" / "agents"


def _split_frontmatter(text: str) -> tuple[dict, str]:
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    assert m, "file is missing YAML frontmatter"
    return yaml.safe_load(m.group(1)), m.group(2)


def test_renders_to_dot_claude_agents(tmp_path: Path) -> None:
    specs = build_registry(default_config())
    paths = render_claude_agents(specs, tmp_path)
    assert len(paths) == len(specs)
    claude_dir = tmp_path / ".claude" / "agents"
    assert claude_dir.is_dir()
    for p in paths:
        assert p.parent == claude_dir
        assert p.suffix == ".md"
        assert p.exists()


def test_every_required_role_written(rendered_dir: Path) -> None:
    from config.schema import REQUIRED_AGENT_ROLES

    for role in REQUIRED_AGENT_ROLES:
        assert (rendered_dir / f"{role}.md").exists(), f"missing {role}.md"


def test_frontmatter_valid_yaml(rendered_dir: Path) -> None:
    for path in sorted(rendered_dir.glob("*.md")):
        meta, body = _split_frontmatter(path.read_text())
        assert "name" in meta
        assert "description" in meta
        assert "tools" in meta
        assert isinstance(meta["tools"], list)
        assert body.strip(), f"empty body for {path.name}"


def test_tool_names_in_frontmatter_developer(rendered_dir: Path) -> None:
    meta, _ = _split_frontmatter((rendered_dir / "developer.md").read_text())
    assert meta["tools"] == ["Read", "Edit", "Write", "Bash", "Glob", "Grep"]


def test_tournament_roles_have_empty_tools(rendered_dir: Path) -> None:
    for role in ("critic_t", "architect_b", "synthesizer", "judge"):
        meta, _ = _split_frontmatter((rendered_dir / f"{role}.md").read_text())
        assert meta["tools"] == []


def test_frontmatter_includes_model(rendered_dir: Path) -> None:
    """Frontmatter exposes model when configured (required by Claude Code)."""
    meta, _ = _split_frontmatter((rendered_dir / "developer.md").read_text())
    assert "model" in meta
    assert meta["model"] in {"sonnet", "opus", "haiku"}


def test_no_backslash_in_paths(tmp_path: Path) -> None:
    """Cross-platform sanity: written paths use forward slashes in relative form."""
    specs = build_registry(default_config())
    paths = render_claude_agents(specs, tmp_path)
    for p in paths:
        rel = p.relative_to(tmp_path).as_posix()
        assert "\\" not in rel


def test_snapshot_developer_md(rendered_dir: Path) -> None:
    """Compare one rendered file byte-for-byte against a golden file.

    On first run, the golden file is created and the test is skipped with a
    message asking the human to review and re-run. Subsequent runs must match.
    """
    actual = (rendered_dir / "developer.md").read_text()
    golden = GOLDEN_DIR / "developer.md"
    if not golden.exists():
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(actual)
        pytest.skip(
            f"created golden file at {golden}; review and re-run to lock the snapshot"
        )
    expected = golden.read_text()
    assert actual == expected, (
        f"rendered developer.md does not match golden at {golden}. "
        "If the change is intentional, delete the golden file and re-run."
    )
