"""``autodev logs`` — tail per-session events.jsonl.

When ``--session`` is omitted, picks the session with the most-recently
modified ``events.jsonl`` (latest run). With ``--follow``, polls the file
every 250 ms after printing existing content; Ctrl-C exits cleanly.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import click
from rich.console import Console

from state.paths import session_events_path, sessions_dir


def _find_latest_session(cwd: Path) -> str | None:
    """Return the session_id whose ``events.jsonl`` has the most recent
    mtime, or ``None`` if no session has any events file yet."""
    root = sessions_dir(cwd)
    if not root.exists():
        return None
    latest_mtime = -1.0
    latest_sid: str | None = None
    for child in root.iterdir():
        if not child.is_dir():
            continue
        evt = child / "events.jsonl"
        if not evt.exists():
            continue
        try:
            mtime = evt.stat().st_mtime
        except OSError:
            continue
        if mtime > latest_mtime:
            latest_mtime = mtime
            latest_sid = child.name
    return latest_sid


def _tail_follow(path: Path, console: Console, poll_s: float = 0.25) -> None:
    """Print new bytes appended to ``path`` as they appear. Blocks until
    Ctrl-C / KeyboardInterrupt."""
    with path.open("r", encoding="utf-8") as fh:
        # Seek to end so we only emit *new* content from here on.
        fh.seek(0, 2)
        try:
            while True:
                chunk = fh.read()
                if chunk:
                    click.echo(chunk, nl=False)
                else:
                    time.sleep(poll_s)
        except KeyboardInterrupt:
            console.print("\n[dim]autodev logs: stopped (Ctrl-C).[/dim]")


@click.command("logs")
@click.option(
    "--session",
    "session_id",
    default=None,
    help="Session id to tail. Defaults to the latest session by mtime.",
)
@click.option(
    "--follow",
    "-f",
    is_flag=True,
    help="After printing existing content, poll for new lines (like tail -f).",
)
def logs(session_id: str | None, follow: bool) -> None:
    """Tail events.jsonl for the given session (or the active one)."""
    console = Console()
    cwd = Path.cwd()

    if session_id is None:
        session_id = _find_latest_session(cwd)
        if session_id is None:
            console.print(
                "[red]autodev logs:[/red] no sessions found under "
                ".autodev/sessions/. Run [bold]autodev plan[/bold] or "
                "[bold]autodev execute[/bold] first."
            )
            sys.exit(1)

    path = session_events_path(cwd, session_id)
    if not path.exists():
        console.print(
            f"[red]autodev logs:[/red] no events.jsonl for session "
            f"[bold]{session_id}[/bold] (looked at {path})."
        )
        sys.exit(1)

    # Print existing content.
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        console.print(f"[red]autodev logs:[/red] failed to read {path}: {exc}")
        sys.exit(1)
    if content:
        click.echo(content, nl=False)

    if follow:
        _tail_follow(path, console)
