"""`autodev prune` - GC tournament artifacts."""

from __future__ import annotations

import sys

import click


@click.command("prune")
@click.option(
    "--older-than",
    default="30d",
    help="Age threshold (e.g. 30d, 7d, 24h).",
)
def prune(older_than: str) -> None:
    """Garbage-collect stale tournament artifacts."""
    click.echo("autodev prune: not yet implemented (Phase 10)")
    sys.exit(1)
