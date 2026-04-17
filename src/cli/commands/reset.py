"""`autodev reset` - clear plan state."""

from __future__ import annotations

import sys

import click


@click.command("reset")
@click.option("--hard", is_flag=True, help="Also remove evidence and tournaments.")
def reset(hard: bool) -> None:
    """Clear .autodev/plan* (destructive)."""
    click.echo("autodev reset: not yet implemented (Phase 4)")
    sys.exit(1)
