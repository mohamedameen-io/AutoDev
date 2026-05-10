"""``autodev resume`` — re-enter the execute loop from the last ledger checkpoint."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from typing import Literal, cast

from adapters.base import PlatformAdapter
from adapters.detect import get_adapter
from agents import build_registry
from autologging import get_logger
from config.loader import load_config
from errors import AutodevError
from orchestrator import Orchestrator
from state.paths import config_path, index_db_path


logger = get_logger(__name__)


def _maybe_refresh_index(cwd: Path, cfg) -> None:
    """v0.25.0: incremental refresh hook (mirrors execute.py / plan.py)."""
    if not cfg.index_enabled:
        return
    db_path = index_db_path(cwd)
    building_marker = cwd / ".autodev" / "index.db.building"
    if building_marker.exists():
        logger.info("index.skip_async_build_in_progress")
        return
    try:
        from state.file_index import IndexBuilder, _last_indexed_sha

        if not db_path.exists():
            IndexBuilder.build_full(cwd, db_path)
        else:
            IndexBuilder.build_incremental(
                cwd, db_path, since_sha=_last_indexed_sha(db_path)
            )
    except Exception as exc:  # noqa: BLE001 - never block on index failure
        logger.warning("index.refresh_failed", err=str(exc))


@click.command("resume")
@click.option(
    "--platform",
    type=click.Choice(["claude_code", "cursor", "auto"]),
    default=None,
)
def resume(platform: str | None) -> None:
    """Resume execution from the last ledger checkpoint."""
    console = Console()
    cwd = Path.cwd()
    cfg_path = config_path(cwd)
    if not cfg_path.exists():
        console.print(
            f"[red]autodev resume:[/red] {cfg_path} not found. "
            "Run [bold]autodev init[/bold] first."
        )
        sys.exit(1)
    try:
        cfg = load_config(cfg_path)
    except AutodevError as exc:
        console.print(f"[red]autodev resume: config error[/red]: {exc}")
        sys.exit(1)

    # v0.25.0: incremental file/symbol index refresh before Orchestrator
    # construction. The architect retry path (if it fires) sees the latest
    # tracked files via ``IndexQuery``.
    _maybe_refresh_index(cwd, cfg)

    async def _run() -> None:
        platform_pref = platform or cfg.platform  # type: ignore[assignment]

        from orchestrator.inline_state import load_suspend_state

        state = load_suspend_state(cwd)
        adapter: PlatformAdapter
        if state is not None:
            from adapters.inline import InlineAdapter

            adapter = InlineAdapter(
                cwd=cwd,
                platform_hint=cfg.platform if cfg.platform != "auto" else "claude_code",  # type: ignore[arg-type]
            )
            if not adapter.has_pending_response(
                state.pending_task_id, state.pending_role
            ):
                console.print(
                    f"[yellow]Waiting for agent response:[/yellow]\n"
                    f"  Delegation: .autodev/delegations/{state.pending_task_id}-{state.pending_role}.md\n"
                    f"  Response:   .autodev/responses/{state.pending_task_id}-{state.pending_role}.json"
                )
                sys.exit(0)  # Not an error — just waiting
            # Response exists — continue with normal resume using inline adapter
        else:
            adapter = await get_adapter(cast("Literal['claude_code', 'cursor', 'inline', 'auto']", platform_pref))

        registry = build_registry(cfg)
        orch = Orchestrator(cwd=cwd, cfg=cfg, adapter=adapter, registry=registry)
        tasks = await orch.resume()
        _render_resume_summary(console, tasks)

    try:
        asyncio.run(_run())
    except AutodevError as exc:
        console.print(f"[red]autodev resume failed[/red]: {exc}")
        sys.exit(2)


def _render_resume_summary(console: Console, tasks: list) -> None:
    if not tasks:
        console.print(
            "[yellow]Nothing to resume — no pending or in-progress tasks.[/yellow]"
        )
        return
    table = Table(title=f"Resumed ({len(tasks)} tasks)")
    table.add_column("Task", style="cyan")
    table.add_column("Status")
    table.add_column("Retries", justify="right")
    for t in tasks:
        table.add_row(t.id, t.status, str(t.retry_count))
    console.print(table)
