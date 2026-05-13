"""``autodev rewind`` — undo a force-accepted phase.

The multi-phase recovery surface (v0.29.0 Bug 9). When a corrective
auto-accept-after-guardrail-kill pathway force-flips a phase's
``review_status`` to ``"accepted"`` without a real
``phase_review_complete`` event in front of it, the operator needs a
way to roll the plan back to the last phase that was actually
reviewed and re-run the affected phases from a clean slate.

Flags::

    autodev rewind                # detect last stable phase, prompt y/N
    autodev rewind --to-phase 0.5 # explicit target
    autodev rewind --dry-run      # show diff without mutation
    autodev rewind --yes          # skip confirmation prompt

Exit codes follow the suite-wide contract: 0 on success (including the
"already at clean state" no-op), 1 on user error (no plan, no stable
phase to rewind to, target phase not in plan), 2 on unexpected
:class:`AutodevError`.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from config.loader import load_config
from errors import AutodevError
from state.paths import config_path
from state.plan_manager import PlanManager
from state.rewind import (
    RewindDiff,
    apply_rewind,
    compute_rewind_diff,
    detect_last_stable_phase,
)


def _render_diff_table(
    console: Console, diff: RewindDiff, *, dry_run: bool
) -> None:
    """Render the per-phase action table the operator sees before/after.

    Three columns: phase id, count of tasks-to-reset within it, and the
    new ``review_status`` (always ``None`` post-rewind for affected
    phases, dim-grey for the target itself which is preserved). The
    archived-artifacts count appears as a footer line so the table
    stays narrow on terminals.
    """
    title = "Rewind (dry-run)" if dry_run else "Rewound"
    table = Table(
        title=f"{title} — target phase: {diff.target_phase_id}",
        show_header=True,
    )
    table.add_column("Phase", style="cyan")
    table.add_column("Tasks reset", justify="right")
    table.add_column("New review_status", style="dim")

    # Group reset task ids by phase prefix for the table. Task ids of
    # the form "<phase>.<sub>" carry the phase id as a prefix; we
    # split on the LAST dot so phase "0.5" → tasks like "0.5.1" still
    # resolve cleanly.
    by_phase: dict[str, list[str]] = {}
    for tid in diff.task_ids_to_reset:
        # Phase id is everything up to but not including the last dot.
        if "." in tid:
            phase_id = tid.rsplit(".", 1)[0]
        else:
            phase_id = tid
        by_phase.setdefault(phase_id, []).append(tid)

    rendered_phases: set[str] = set()
    for phase_id in diff.phase_ids_to_reset:
        rendered_phases.add(phase_id)
        table.add_row(
            phase_id,
            str(len(by_phase.get(phase_id, []))),
            "None",
        )
    # Phases that have tasks to reset but no review_status flip
    # (e.g. a phase mid-execution with review_status already None).
    for phase_id in by_phase:
        if phase_id in rendered_phases:
            continue
        table.add_row(
            phase_id,
            str(len(by_phase[phase_id])),
            "(unchanged)",
        )

    console.print(table)
    if diff.evidence_paths_to_archive:
        console.print(
            f"[dim]→ {len(diff.evidence_paths_to_archive)} evidence/"
            "tournament artifact(s) will be archived to "
            "[bold].autodev/rewound/<timestamp>-"
            f"{diff.target_phase_id}/[/bold].[/dim]"
        )


@click.command("rewind")
@click.option(
    "--to-phase",
    "to_phase",
    type=str,
    default=None,
    help=(
        "Explicit target phase id (e.g. '0.5'). When omitted, the "
        "command auto-detects the last genuinely-stable phase by "
        "replaying the ledger."
    ),
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    help="Show the planned mutations without writing the ledger.",
)
@click.option(
    "--yes",
    "yes",
    is_flag=True,
    help="Skip the interactive confirmation prompt.",
)
def rewind(to_phase: str | None, dry_run: bool, yes: bool) -> None:
    """Undo a force-accepted phase. Resets later phases to clean state."""
    console = Console()
    cwd = Path.cwd()
    cfg_path = config_path(cwd)
    if not cfg_path.exists():
        console.print(
            f"[red]autodev rewind:[/red] {cfg_path} not found. "
            "Run [bold]autodev init[/bold] first."
        )
        sys.exit(1)
    try:
        load_config(cfg_path)
    except AutodevError as exc:
        console.print(f"[red]autodev rewind: config error[/red]: {exc}")
        sys.exit(1)

    # Resolve target phase. ``--to-phase`` wins; otherwise auto-detect.
    if to_phase is None:
        detected = detect_last_stable_phase(cwd)
        if detected is None:
            console.print(
                "[red]autodev rewind:[/red] no genuinely-accepted phase "
                "found in the ledger to rewind to. The plan has either "
                "never reached an accepted phase, or every acceptance "
                "was a force-accept lacking a matching "
                "[bold]phase_review_complete[/bold] event."
            )
            console.print(
                "[dim]Hint: pass [bold]--to-phase ID[/bold] to rewind to "
                "an explicit target, or run [bold]autodev reset[/bold] "
                "to clear plan state entirely.[/dim]"
            )
            sys.exit(1)
        target_phase_id: str = detected
    else:
        target_phase_id = to_phase

    diff = compute_rewind_diff(cwd, target_phase_id)
    if (
        not diff.task_ids_to_reset
        and not diff.phase_ids_to_reset
        and not diff.evidence_paths_to_archive
    ):
        console.print(
            f"[yellow]autodev rewind:[/yellow] nothing to rewind — "
            f"plan is already at or before phase "
            f"[bold]{target_phase_id}[/bold]."
        )
        return

    _render_diff_table(console, diff, dry_run=dry_run)

    if dry_run:
        console.print(
            "[cyan]autodev rewind --dry-run:[/cyan] no ledger entries "
            "written; no artifacts archived."
        )
        return

    if not yes:
        confirmed = click.confirm(
            f"Rewind to phase {target_phase_id}? "
            f"({len(diff.task_ids_to_reset)} task(s), "
            f"{len(diff.phase_ids_to_reset)} phase(s), "
            f"{len(diff.evidence_paths_to_archive)} artifact(s))",
            default=False,
        )
        if not confirmed:
            console.print("[yellow]autodev rewind:[/yellow] aborted.")
            return

    async def _run() -> None:
        pm = PlanManager(cwd, session_id="cli-rewind")
        result = await apply_rewind(cwd, target_phase_id, pm)
        archive_note = (
            f" archived {len(result.archived_paths)} artifact(s) to "
            f"[bold]{result.archive_dir.relative_to(cwd)}[/bold]"
            if result.archive_dir is not None
            else ""
        )
        console.print(
            f"[green]autodev rewind:[/green] reset "
            f"{len(result.reset_task_ids)} task(s) and "
            f"{len(result.reset_phase_ids)} phase(s);"
            f"{archive_note}."
        )

    try:
        asyncio.run(_run())
    except AutodevError as exc:
        console.print(f"[red]autodev rewind failed[/red]: {exc}")
        sys.exit(2)
