"""CLI entry point."""

from __future__ import annotations

import click

from _version import __version__
from cli.commands import register_commands


@click.group(
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=False,
)
@click.version_option(version=__version__, prog_name="autodev")
def cli() -> None:
    """autodev: multi-agent orchestrator with tournament self-refinement."""


register_commands(cli)


def main() -> None:
    """Console-script entry point."""
    cli(standalone_mode=True)
