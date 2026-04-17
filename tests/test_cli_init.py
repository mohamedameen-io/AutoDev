"""Tests for ``autodev init`` CLI command."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from cli import cli
from config.schema import REQUIRED_AGENT_ROLES


def _invoke_init(runner: CliRunner, tmp_path: Path, *args: str):
    """Run ``autodev init`` in an isolated filesystem rooted at tmp_path."""
    with runner.isolated_filesystem(temp_dir=tmp_path) as cwd:
        result = runner.invoke(cli, ["init", *args], catch_exceptions=False)
        return result, Path(cwd)


def test_init_creates_autodev_dir(tmp_path: Path) -> None:
    runner = CliRunner()
    result, cwd = _invoke_init(runner, tmp_path)
    assert result.exit_code == 0, result.output

    autodev_dir = cwd / ".autodev"
    assert (autodev_dir / "config.json").exists()
    assert (autodev_dir / "spec.md").exists()

    claude_dir = cwd / ".claude" / "agents"
    cursor_dir = cwd / ".cursor" / "rules"
    for role in REQUIRED_AGENT_ROLES:
        assert (claude_dir / f"{role}.md").exists(), f"missing claude/{role}.md"
        assert (cursor_dir / f"{role}.mdc").exists(), f"missing cursor/{role}.mdc"


def test_init_fails_if_exists_no_force(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as cwd:
        first = runner.invoke(cli, ["init"], catch_exceptions=False)
        assert first.exit_code == 0, first.output

        second = runner.invoke(cli, ["init"], catch_exceptions=False)
        assert second.exit_code != 0
        assert "already exists" in second.output.lower()


def test_init_with_force_overwrites(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as cwd:
        first = runner.invoke(cli, ["init"], catch_exceptions=False)
        assert first.exit_code == 0, first.output

        # mutate the spec and make sure --force rewrites it
        spec = Path(cwd) / ".autodev" / "spec.md"
        spec.write_text("user-edited content")

        second = runner.invoke(cli, ["init", "--force"], catch_exceptions=False)
        assert second.exit_code == 0, second.output
        # The generated template reasserts itself
        assert "user-edited" not in spec.read_text()


def test_init_platform_flag_claude(tmp_path: Path) -> None:
    runner = CliRunner()
    result, cwd = _invoke_init(runner, tmp_path, "--platform", "claude")
    assert result.exit_code == 0, result.output
    config = json.loads((cwd / ".autodev" / "config.json").read_text())
    assert config["platform"] == "claude_code"


def test_init_platform_flag_cursor(tmp_path: Path) -> None:
    runner = CliRunner()
    result, cwd = _invoke_init(runner, tmp_path, "--platform", "cursor")
    assert result.exit_code == 0, result.output
    config = json.loads((cwd / ".autodev" / "config.json").read_text())
    assert config["platform"] == "cursor"


def test_init_platform_flag_auto(tmp_path: Path) -> None:
    runner = CliRunner()
    result, cwd = _invoke_init(runner, tmp_path, "--platform", "auto")
    assert result.exit_code == 0, result.output
    config = json.loads((cwd / ".autodev" / "config.json").read_text())
    assert config["platform"] == "auto"


def test_init_config_passes_validation(tmp_path: Path) -> None:
    """The written config must round-trip through the loader."""
    from config.loader import load_config

    runner = CliRunner()
    result, cwd = _invoke_init(runner, tmp_path)
    assert result.exit_code == 0, result.output
    cfg = load_config(cwd / ".autodev" / "config.json")
    cfg.require_all_roles()
