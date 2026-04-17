"""Tests for :mod:`src.agents.render_cursor`."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from agents import build_registry
from agents.render_cursor import render_cursor_rules
from config.defaults import default_config


GOLDEN_DIR = Path(__file__).parent / "golden" / "cursor_rules"


@pytest.fixture
def rendered_dir(tmp_path: Path) -> Path:
    specs = build_registry(default_config())
    render_cursor_rules(specs, tmp_path)
    return tmp_path / ".cursor" / "rules"


def _split_frontmatter(text: str) -> tuple[dict, str]:
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    assert m, "file is missing YAML frontmatter"
    return yaml.safe_load(m.group(1)), m.group(2)


def test_renders_to_dot_cursor_rules(tmp_path: Path) -> None:
    specs = build_registry(default_config())
    paths = render_cursor_rules(specs, tmp_path)
    assert len(paths) == len(specs)
    rules_dir = tmp_path / ".cursor" / "rules"
    assert rules_dir.is_dir()
    for p in paths:
        assert p.parent == rules_dir
        assert p.suffix == ".mdc"
        assert p.exists()


def test_every_required_role_written(rendered_dir: Path) -> None:
    from config.schema import REQUIRED_AGENT_ROLES

    for role in REQUIRED_AGENT_ROLES:
        assert (rendered_dir / f"{role}.mdc").exists(), f"missing {role}.mdc"


def test_frontmatter_valid_mdc(rendered_dir: Path) -> None:
    """MDC frontmatter must have description + alwaysApply keys."""
    for path in sorted(rendered_dir.glob("*.mdc")):
        meta, body = _split_frontmatter(path.read_text())
        assert "description" in meta
        assert "alwaysApply" in meta
        assert isinstance(meta["alwaysApply"], bool)
        assert body.strip(), f"empty body for {path.name}"


def test_body_starts_with_role_heading(rendered_dir: Path) -> None:
    """Body starts with a '# <role> role' heading."""
    _, body = _split_frontmatter((rendered_dir / "developer.mdc").read_text())
    assert body.lstrip().startswith("# developer role")


def test_no_backslash_in_paths(tmp_path: Path) -> None:
    specs = build_registry(default_config())
    paths = render_cursor_rules(specs, tmp_path)
    for p in paths:
        rel = p.relative_to(tmp_path).as_posix()
        assert "\\" not in rel


def test_snapshot_developer_mdc(rendered_dir: Path) -> None:
    """Compare one rendered .mdc file byte-for-byte against a golden file."""
    actual = (rendered_dir / "developer.mdc").read_text()
    golden = GOLDEN_DIR / "developer.mdc"
    if not golden.exists():
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(actual)
        pytest.skip(
            f"created golden file at {golden}; review and re-run to lock the snapshot"
        )
    expected = golden.read_text()
    assert actual == expected, (
        f"rendered developer.mdc does not match golden at {golden}. "
        "If the change is intentional, delete the golden file and re-run."
    )
