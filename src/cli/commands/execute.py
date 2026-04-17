"""``autodev execute`` — run the EXECUTE phase."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from typing import Literal, cast

from adapters.detect import get_adapter
from adapters.inline_types import DelegationPendingSignal
from agents import build_registry
from config.loader import load_config
from errors import AutodevError
from orchestrator import Orchestrator
from state.paths import config_path


@click.command("execute")
@click.option("--task", "task_id", default=None, help="Target a specific task id.")
@click.option("--dry-run", is_flag=True, help="Plan work without mutating the repo.")
@click.option(
    "--no-impl-tournament",
    is_flag=True,
    help="Disable the implementation tournament (Phase 7 once integrated).",
)
@click.option(
    "--platform",
    type=click.Choice(["claude_code", "cursor", "auto"]),
    default=None,
)
def execute(
    task_id: str | None,
    dry_run: bool,
    no_impl_tournament: bool,
    platform: str | None,
) -> None:
    """Execute pending tasks serially (developer -> review -> tests -> advance)."""
    console = Console()
    cwd = Path.cwd()
    cfg_path = config_path(cwd)
    if not cfg_path.exists():
        console.print(
            f"[red]autodev execute:[/red] {cfg_path} not found. "
            "Run [bold]autodev init[/bold] first."
        )
        sys.exit(1)
    try:
        cfg = load_config(cfg_path)
    except AutodevError as exc:
        console.print(f"[red]autodev execute: config error[/red]: {exc}")
        sys.exit(1)

    if dry_run:
        console.print(
            "[yellow]--dry-run not yet implemented; no work will be done.[/yellow]"
        )
        sys.exit(0)

    async def _run() -> None:
        platform_pref = platform or cfg.platform  # type: ignore[assignment]
        adapter = await get_adapter(cast("Literal['claude_code', 'cursor', 'inline', 'auto']", platform_pref))
        registry = build_registry(cfg)
        orch = Orchestrator(
            cwd=cwd,
            cfg=cfg,
            adapter=adapter,
            registry=registry,
            disable_impl_tournament=no_impl_tournament,
        )
        try:
            tasks = await orch.execute(task_id=task_id)
            _render_execute_summary(console, tasks)
        except DelegationPendingSignal as sig:
            console.print(
                f"[yellow]Delegation written:[/yellow] {sig.delegation_path}\n"
                f"[yellow]Agent must respond, then run:[/yellow] autodev resume"
            )
            # Exit 0 — this is a normal inline exit, not an error.

    try:
        asyncio.run(_run())
    except AutodevError as exc:
        console.print(f"[red]autodev execute failed[/red]: {exc}")
        sys.exit(2)


def _render_execute_summary(console: Console, tasks: list) -> None:
    if not tasks:
        console.print("[yellow]No tasks to execute.[/yellow]")
        return
    table = Table(title=f"Execute results ({len(tasks)} tasks)")
    table.add_column("Task", style="cyan")
    table.add_column("Status")
    table.add_column("Retries", justify="right")
    table.add_column("Escalated")
    for t in tasks:
        status_color = {
            "complete": "green",
            "blocked": "red",
            "skipped": "yellow",
        }.get(t.status, "white")
        table.add_row(
            t.id,
            f"[{status_color}]{t.status}[/{status_color}]",
            str(t.retry_count),
            "yes" if t.escalated else "no",
        )
    console.print(table)
