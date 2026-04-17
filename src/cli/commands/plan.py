"""``autodev plan`` — run the PLAN phase end-to-end."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from typing import Literal, cast

from adapters.detect import get_adapter
from agents import build_registry
from config.loader import load_config
from errors import AutodevError
from orchestrator import Orchestrator
from state.paths import config_path


@click.command("plan")
@click.argument("intent", required=True)
@click.option(
    "--platform",
    type=click.Choice(["claude_code", "cursor", "auto"]),
    default=None,
    help="Override platform selection (else use config + auto-detect).",
)
def plan(intent: str, platform: str | None) -> None:
    """Run PLAN phase: explore, research, draft, gate, persist."""
    console = Console()
    cwd = Path.cwd()
    cfg_path = config_path(cwd)
    if not cfg_path.exists():
        console.print(
            f"[red]autodev plan:[/red] {cfg_path} not found. "
            "Run [bold]autodev init[/bold] first."
        )
        sys.exit(1)
    try:
        cfg = load_config(cfg_path)
    except AutodevError as exc:
        console.print(f"[red]autodev plan: config error[/red]: {exc}")
        sys.exit(1)

    async def _run() -> None:
        platform_pref = platform or cfg.platform  # type: ignore[assignment]
        adapter = await get_adapter(cast("Literal['claude_code', 'cursor', 'inline', 'auto']", platform_pref))
        registry = build_registry(cfg)
        orch = Orchestrator(cwd=cwd, cfg=cfg, adapter=adapter, registry=registry)
        approved = await orch.plan(intent)
        _render_plan_summary(console, approved)

    try:
        asyncio.run(_run())
    except AutodevError as exc:
        console.print(f"[red]autodev plan failed[/red]: {exc}")
        sys.exit(2)


def _render_plan_summary(console: Console, plan_obj) -> None:
    table = Table(title=f"Plan approved: {plan_obj.metadata.get('title', plan_obj.plan_id)}")
    table.add_column("Phase", style="cyan")
    table.add_column("Task", style="cyan")
    table.add_column("Title")
    table.add_column("Files")
    for phase in plan_obj.phases:
        for task in phase.tasks:
            table.add_row(
                f"{phase.id}",
                task.id,
                task.title,
                ", ".join(task.files) if task.files else "-",
            )
    console.print(table)
    console.print(f"[green]Plan persisted:[/green] {plan_obj.plan_id}")
