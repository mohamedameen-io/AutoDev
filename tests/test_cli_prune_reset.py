"""Tests for ``autodev prune`` and ``autodev reset`` CLI commands."""

from __future__ import annotations


from click.testing import CliRunner

from cli import cli


# ---------------------------------------------------------------------------
# Prune tests
# ---------------------------------------------------------------------------


def test_prune_exits_1_not_implemented() -> None:
    """Prune is a stub and should exit 1 with 'not yet implemented'."""
    runner = CliRunner()
    result = runner.invoke(cli, ["prune"])
    assert result.exit_code == 1
    assert "not yet implemented" in result.output


def test_prune_with_older_than_flag() -> None:
    """Prune accepts --older-than flag but still exits 1."""
    runner = CliRunner()
    result = runner.invoke(cli, ["prune", "--older-than", "7d"])
    assert result.exit_code == 1
    assert "not yet implemented" in result.output


def test_prune_help_shows_option() -> None:
    """Prune --help should document the --older-than option."""
    runner = CliRunner()
    result = runner.invoke(cli, ["prune", "--help"])
    assert result.exit_code == 0
    assert "--older-than" in result.output


# ---------------------------------------------------------------------------
# Reset tests
# ---------------------------------------------------------------------------


def test_reset_exits_1_not_implemented() -> None:
    """Reset is a stub and should exit 1 with 'not yet implemented'."""
    runner = CliRunner()
    result = runner.invoke(cli, ["reset"])
    assert result.exit_code == 1
    assert "not yet implemented" in result.output


def test_reset_with_hard_flag() -> None:
    """Reset accepts --hard flag but still exits 1."""
    runner = CliRunner()
    result = runner.invoke(cli, ["reset", "--hard"])
    assert result.exit_code == 1
    assert "not yet implemented" in result.output


def test_reset_help_shows_option() -> None:
    """Reset --help should document the --hard option."""
    runner = CliRunner()
    result = runner.invoke(cli, ["reset", "--help"])
    assert result.exit_code == 0
    assert "--hard" in result.output
