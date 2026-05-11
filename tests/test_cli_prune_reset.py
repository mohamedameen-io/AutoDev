"""``--help`` smoke tests for ``autodev prune`` and ``autodev reset``.

Functional coverage of these two commands lives in
:mod:`tests.test_cli_prune` and :mod:`tests.test_cli_reset` (both added
in v0.25.2 when the stubs were replaced by real implementations). The
``not yet implemented`` exit-1 assertions that were here are gone.
"""

from __future__ import annotations


from click.testing import CliRunner

from cli import cli


def test_prune_help_shows_option() -> None:
    """``prune --help`` documents the ``--older-than`` option."""
    runner = CliRunner()
    result = runner.invoke(cli, ["prune", "--help"])
    assert result.exit_code == 0
    assert "--older-than" in result.output


def test_reset_help_shows_option() -> None:
    """``reset --help`` documents the ``--hard`` option."""
    runner = CliRunner()
    result = runner.invoke(cli, ["reset", "--help"])
    assert result.exit_code == 0
    assert "--hard" in result.output
