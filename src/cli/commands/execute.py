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
from agents import build_registry
from autologging import get_logger
from config.loader import load_config
from errors import AutodevError
from orchestrator import Orchestrator
from state.paths import config_path, index_db_path
from state.plan_manager import PlanManager
from state.schemas import Plan, Task


logger = get_logger(__name__)


def _maybe_refresh_index(cwd: Path, cfg) -> None:
    """v0.25.0: incremental refresh hook shared across execute/plan/resume.

    Skips silently when:
      * ``cfg.index_enabled`` is False
      * ``.autodev/index.db.building`` exists (async build in progress)

    Else builds full index when missing, otherwise runs incremental refresh
    keyed off the persisted ``last_indexed_sha``. Failures are logged and
    swallowed — the orchestrator must continue even if the index is stale
    (the architect just gets an emptier candidate-files block).
    """
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
        # v0.25.2: render plan + dispatch windows WITHOUT invoking any agent.
        # Preview only; useful for validating depends_on ordering and
        # confirming the architect's plan before committing real LLM spend.
        _exit = _render_dry_run(console, cwd, cfg)
        sys.exit(_exit)

    # v0.25.0: incremental file/symbol index refresh. Runs before
    # Orchestrator instantiation so the planner sees the latest tracked
    # files when it queries ``IndexQuery.get_candidates_for_spec``.
    _maybe_refresh_index(cwd, cfg)

    async def _run() -> None:
        # v0.26.0: ``platform: inline`` is auto-migrated to ``claude_code``
        # by the schema validator. ``cfg.platform`` is always one of
        # {claude_code, cursor, auto} here; no ``DelegationPendingSignal``
        # path remains.
        platform_pref = platform or cfg.platform  # type: ignore[assignment]
        adapter = await get_adapter(
            cast("Literal['claude_code', 'cursor', 'auto']", platform_pref)
        )
        registry = build_registry(cfg)
        orch = Orchestrator(
            cwd=cwd,
            cfg=cfg,
            adapter=adapter,
            registry=registry,
            disable_impl_tournament=no_impl_tournament,
        )
        tasks = await orch.execute(task_id=task_id)
        _render_execute_summary(console, tasks)

    try:
        asyncio.run(_run())
    except AutodevError as exc:
        console.print(f"[red]autodev execute failed[/red]: {exc}")
        sys.exit(2)


def _render_dry_run(console: Console, cwd: Path, cfg) -> int:
    """v0.25.2: render plan preview + per-phase dispatch windows.

    Returns the intended process exit code (0 on success, 1 if no plan).
    Does NOT invoke any agent adapter — preview only.
    """
    pm = PlanManager(cwd, session_id="execute-dry-run")
    plan: Plan | None = asyncio.run(pm.load())
    if plan is None:
        console.print(
            "[red]autodev execute --dry-run:[/red] no plan on disk. "
            "Run [bold]autodev plan '<intent>'[/bold] first."
        )
        return 1

    title = plan.metadata.get("title", plan.plan_id) if plan.metadata else plan.plan_id
    console.print(f"[cyan]Plan:[/cyan] {title}")

    task_table = Table(
        title="Tasks (preview — no LLM calls will be made)",
        show_header=True,
        header_style="bold cyan",
    )
    task_table.add_column("Phase", justify="right")
    task_table.add_column("Task", style="cyan")
    task_table.add_column("Title")
    task_table.add_column("Depends", style="dim")
    task_table.add_column("Status", style="dim")
    for phase in plan.phases:
        for t in phase.tasks:
            deps = ",".join(t.depends_on) if t.depends_on else "—"
            task_table.add_row(phase.id, t.id, t.title, deps, t.status)
    console.print(task_table)

    parallelism = (
        cfg.tournaments.execute_max_parallel_tasks
        if getattr(cfg.tournaments, "execute_max_parallel_tasks", None)
        else 4
    )
    console.print(
        f"\n[bold]Dispatch order[/bold] (parallelism={parallelism}, "
        "roles per task: developer → reviewer → test_engineer):"
    )
    for phase in plan.phases:
        windows = _compute_dispatch_windows(phase.tasks, parallelism)
        if not windows:
            console.print(f"  Phase {phase.id}: [dim](no pending tasks)[/dim]")
            continue
        for i, window in enumerate(windows, start=1):
            label = ", ".join(t.id for t in window)
            console.print(f"  Phase {phase.id} · window {i}: [{label}]")
    return 0


def _compute_dispatch_windows(
    tasks: list[Task], parallelism: int
) -> list[list[Task]]:
    """Group ``tasks`` into successive parallelism windows respecting
    ``depends_on`` within the same phase.

    A task lands in the first window whose preceding windows already
    contain all of its in-phase dependencies. Inter-phase dependencies
    are ignored here (the phase boundary itself enforces them).
    """
    phase_ids = {t.id for t in tasks}
    ready: list[Task] = list(tasks)
    placed: set[str] = set()
    windows: list[list[Task]] = []
    while ready:
        window: list[Task] = []
        rest: list[Task] = []
        for t in ready:
            in_phase_deps = [d for d in t.depends_on if d in phase_ids]
            if all(d in placed for d in in_phase_deps) and len(window) < parallelism:
                window.append(t)
            else:
                rest.append(t)
        if not window:
            # Unsatisfiable dependency (cycle or external); flush the rest
            # into one final window so we don't loop forever.
            windows.append(rest)
            break
        for t in window:
            placed.add(t.id)
        windows.append(window)
        ready = rest
    return windows


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
