"""Smoke tests for the click CLI."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from _version import __version__
from cli import cli


EXPECTED_SUBCOMMANDS = {
    "init",
    "plan",
    "execute",
    "resume",
    "status",
    "tournament",
    "doctor",
    "logs",
    "reset",
    "prune",
}


def test_help_works() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    for name in EXPECTED_SUBCOMMANDS:
        assert name in result.output, f"missing subcommand in help: {name}"


def test_version_works() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_doctor_in_empty_dir(tmp_path: Path) -> None:
    """Doctor should not traceback in a dir without .autodev/."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(cli, ["doctor"], catch_exceptions=False)
    # Exit 1 is acceptable (CLIs may not be available), but no crash.
    assert result.exit_code in (0, 1)
    assert "autodev doctor" in result.output


def test_logs_outside_project_exits_clean(tmp_path: Path) -> None:
    """``autodev logs`` outside a project (no .autodev/sessions/) exits 1
    with an actionable message — NOT a stack trace or 'not yet
    implemented' (v0.25.2 replaced the stub).
    """
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(cli, ["logs"])
    assert result.exit_code == 1
    assert "not yet implemented" not in result.output
    assert "no session" in result.output.lower()


def test_resume_and_status_require_project(tmp_path: Path) -> None:
    """``resume`` / ``status`` outside a project exit with a pointer to init."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        for name in ("resume", "status"):
            result = runner.invoke(cli, [name])
            assert result.exit_code == 1, f"{name} should exit 1 without .autodev/"
            assert "autodev init" in result.output
