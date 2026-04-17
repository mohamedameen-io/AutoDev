"""``autodev status`` — show the current plan and task states."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from config.loader import load_config
from errors import AutodevError
from state.evidence import list_evidence
from state.knowledge import KnowledgeStore
from state.paths import config_path
from state.plan_manager import PlanManager


@click.command("status")
def status() -> None:
    """Print a table of plan + task states + evidence counts."""
    console = Console()
    cwd = Path.cwd()
    cfg_path = config_path(cwd)
    if not cfg_path.exists():
        console.print(
            f"[red]autodev status:[/red] {cfg_path} not found. "
            "Run [bold]autodev init[/bold] first."
        )
        sys.exit(1)
    try:
        cfg = load_config(cfg_path)
    except AutodevError as exc:
        console.print(f"[red]autodev status: config error[/red]: {exc}")
        sys.exit(1)

    async def _run() -> None:
        pm = PlanManager(cwd, session_id="status-readonly")
        plan = await pm.load()
        # Always surface the Knowledge summary — even with no plan it's useful
        # to see how many lessons the store holds before running work.
        ks = KnowledgeStore(cwd, cfg=cfg)
        try:
            swarm_entries = await ks.read_all(tier="swarm")
            hive_entries = await ks.read_all(tier="hive") if ks.hive_enabled else []
        except Exception:  # pragma: no cover - display only
            swarm_entries, hive_entries = [], []

        if plan is None:
            console.print(
                "[yellow]No plan yet.[/yellow] Run [bold]autodev plan "
                "'<intent>'[/bold] to create one."
            )
            _print_knowledge_summary(console, len(swarm_entries), len(hive_entries))
            return
        console.print(
            f"[cyan]Plan:[/cyan] {plan.metadata.get('title', plan.plan_id)} "
            f"[dim]({plan.plan_id})[/dim]"
        )
        table = Table(title="Tasks")
        table.add_column("Phase", style="cyan")
        table.add_column("Task", style="cyan")
        table.add_column("Status")
        table.add_column("Retries", justify="right")
        table.add_column("Evidence", justify="right")
        totals = {
            "pending": 0,
            "in_progress": 0,
            "complete": 0,
            "blocked": 0,
            "skipped": 0,
        }
        for phase in plan.phases:
            for task in phase.tasks:
                ev = await list_evidence(cwd, task.id)
                totals[task.status] = totals.get(task.status, 0) + 1
                table.add_row(
                    phase.id,
                    task.id,
                    task.status,
                    str(task.retry_count),
                    str(len(ev)),
                )
        console.print(table)
        summary = " | ".join(f"{k}={v}" for k, v in totals.items())
        console.print(f"[dim]{summary}[/dim]")
        _print_knowledge_summary(console, len(swarm_entries), len(hive_entries))

    try:
        asyncio.run(_run())
    except AutodevError as exc:
        console.print(f"[red]autodev status failed[/red]: {exc}")
        sys.exit(2)


def _print_knowledge_summary(console: Console, swarm_count: int, hive_count: int) -> None:
    """Render the Knowledge section. Purely informational — never changes exit code."""
    console.print(
        f"[cyan]Knowledge:[/cyan] {swarm_count} lessons in swarm tier, "
        f"{hive_count} in hive tier"
    )
