"""`autodev logs` - tail events.jsonl for a session."""

from __future__ import annotations

import sys

import click


@click.command("logs")
@click.option("--session", "session_id", default=None, help="Session id to tail.")
def logs(session_id: str | None) -> None:
    """Tail events.jsonl for the given session (or the active one)."""
    click.echo("autodev logs: not yet implemented (Phase 4)")
    sys.exit(1)
