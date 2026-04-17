"""Tests for `autodev plugins` CLI command."""

from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner

from cli.commands.plugins import plugins
from plugins.registry import PluginRegistry


def test_plugins_no_plugins_discovered() -> None:
    runner = CliRunner()
    with patch(
        "cli.commands.plugins.discover_plugins",
        return_value=PluginRegistry(),
    ):
        result = runner.invoke(plugins)
    assert result.exit_code == 0
    assert "No plugins discovered" in result.output


def test_plugins_with_qa_gate() -> None:
    from plugins.registry import QAContext, GateResult

    class _Gate:
        name = "my_gate"

        async def run(self, ctx: QAContext) -> GateResult:
            return GateResult(passed=True)

    reg = PluginRegistry()
    reg.qa_gates["my_gate"] = _Gate()  # type: ignore[assignment]

    runner = CliRunner()
    with patch("cli.commands.plugins.discover_plugins", return_value=reg):
        result = runner.invoke(plugins)
    assert result.exit_code == 0
    assert "my_gate" in result.output
    assert "QA Gate" in result.output


def test_plugins_with_multiple_types() -> None:
    from typing import Any
    from plugins.registry import QAContext, GateResult

    class _Gate:
        name = "gate1"

        async def run(self, ctx: QAContext) -> GateResult:
            return GateResult(passed=True)

    class _Judge:
        name = "judge1"

        async def rank(self, task: str, versions: list[Any]) -> list[str]:
            return versions

    class _Agent:
        name = "agent1"

        def get_spec(self) -> Any:
            return None

        def render_platform(self, platform: str) -> str:
            return ""

    reg = PluginRegistry()
    reg.qa_gates["gate1"] = _Gate()  # type: ignore[assignment]
    reg.judges["judge1"] = _Judge()  # type: ignore[assignment]
    reg.agents["agent1"] = _Agent()  # type: ignore[assignment]

    runner = CliRunner()
    with patch("cli.commands.plugins.discover_plugins", return_value=reg):
        result = runner.invoke(plugins)
    assert result.exit_code == 0
    assert "gate1" in result.output
    assert "judge1" in result.output
    assert "agent1" in result.output
    assert "3" in result.output  # total count


def test_plugins_command_registered_in_cli() -> None:
    """The plugins command is accessible from the top-level CLI group."""
    from cli import cli

    runner = CliRunner()
    result = runner.invoke(cli, ["plugins", "--help"])
    assert result.exit_code == 0
    assert "plugins" in result.output.lower()
