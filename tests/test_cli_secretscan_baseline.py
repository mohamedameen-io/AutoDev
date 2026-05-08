"""Tests for v0.19.0 ``autodev secretscan baseline`` CLI subcommand."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from cli import cli as cli_root


def test_secretscan_baseline_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_root, ["secretscan", "--help"])
    assert result.exit_code == 0
    assert "baseline" in result.output


def test_secretscan_baseline_writes_sidecar(tmp_path: Path) -> None:
    secret = "AKIAABCDEFGHIJKLMNOP"
    (tmp_path / "leaked.py").write_text(f'KEY = "{secret}"\n')

    runner = CliRunner()
    result = runner.invoke(
        cli_root, ["secretscan", "baseline", "--cwd", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    sidecar = tmp_path / ".autodev" / "secretscan-baseline.json"
    assert sidecar.exists()
    raw = json.loads(sidecar.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert any("AWS access key" in k for k in raw["keys"])


def test_secretscan_baseline_reports_count(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('hi')\n")
    runner = CliRunner()
    result = runner.invoke(
        cli_root, ["secretscan", "baseline", "--cwd", str(tmp_path)]
    )
    assert result.exit_code == 0
    assert "Baseline written" in result.output
    assert "0 keys" in result.output
