"""Tests for `autodev doctor` plugins and guardrails sections."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from cli.commands.doctor import doctor
from config.defaults import default_config
from config.loader import save_config
from plugins.registry import PluginRegistry


def _write_config(tmp_path: Path) -> Path:
    cfg_path = tmp_path / ".autodev" / "config.json"
    save_config(default_config(), cfg_path)
    return cfg_path


def test_doctor_shows_plugins_section(tmp_path: Path) -> None:
    _write_config(tmp_path)
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        # Copy config into isolated filesystem cwd.
        import shutil
        import os
        cwd = Path(os.getcwd())
        (cwd / ".autodev").mkdir(parents=True, exist_ok=True)
        shutil.copy(tmp_path / ".autodev" / "config.json", cwd / ".autodev" / "config.json")

        with patch(
            "cli.commands.doctor.discover_plugins",
            return_value=PluginRegistry(),
        ):
            result = runner.invoke(doctor, catch_exceptions=False)

    # Should show Plugins table header.
    assert "Plugins" in result.output
    assert "QA Gates" in result.output
    assert "Judge Providers" in result.output
    assert "Agent Extensions" in result.output


def test_doctor_shows_guardrails_section(tmp_path: Path) -> None:
    _write_config(tmp_path)
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        import shutil
        import os
        cwd = Path(os.getcwd())
        (cwd / ".autodev").mkdir(parents=True, exist_ok=True)
        shutil.copy(tmp_path / ".autodev" / "config.json", cwd / ".autodev" / "config.json")

        with patch(
            "cli.commands.doctor.discover_plugins",
            return_value=PluginRegistry(),
        ):
            result = runner.invoke(doctor, catch_exceptions=False)

    assert "Guardrails" in result.output
    assert "max_tool_calls_per_task" in result.output
    assert "max_duration_s_per_task" in result.output
    assert "max_diff_bytes" in result.output


def test_doctor_plugins_counts_shown(tmp_path: Path) -> None:
    from plugins.registry import QAContext, GateResult

    class _Gate:
        name = "g1"

        async def run(self, ctx: QAContext) -> GateResult:
            return GateResult(passed=True)

    reg = PluginRegistry()
    reg.qa_gates["g1"] = _Gate()  # type: ignore[assignment]

    _write_config(tmp_path)
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        import shutil
        import os
        cwd = Path(os.getcwd())
        (cwd / ".autodev").mkdir(parents=True, exist_ok=True)
        shutil.copy(tmp_path / ".autodev" / "config.json", cwd / ".autodev" / "config.json")

        with patch("cli.commands.doctor.discover_plugins", return_value=reg):
            result = runner.invoke(doctor, catch_exceptions=False)

    # QA Gates count should be 1.
    assert "1" in result.output
