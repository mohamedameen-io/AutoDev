"""``autodev requeue`` — flip blocked tasks back to ``pending``.

The infrastructure-failure recovery surface (v0.28.0 Bug 8). When a
task gets stuck in ``status="blocked"`` because of a transient outside-
the-loop failure (auth refresh required, gateway 4xx, DNS hiccup,
network blip), the operator needs a way to put the task back on the
runnable queue without losing the surrounding plan structure or the
prior tournament work invested in it.

Selection flags (mutually compose with implicit OR):

  ``--task ID``           explicit task id; may repeat
  ``--phase ID``          every blocked task in the phase; may repeat
  ``--infrastructure``    every blocked task whose ``blocked_reason``
                          matches the v0.28.0 keyword heuristic (403,
                          401, Forbidden, authenticate, ...). Bug 6 in
                          v0.29.0 makes this typed via
                          ``block_reason_class``.
  ``--all-blocked``       every blocked task in the plan

UX flags:

  ``--yes``      skip the interactive confirmation prompt
  ``--dry-run``  print the planned mutations without writing the ledger

Exit codes follow the suite-wide contract: 0 on success (including the
"nothing to requeue" no-op), 1 on user error (no plan, unknown task
id, no selector + non-interactive shell), 2 on unexpected
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
from state.infra_patterns import (
    INFRA_PATTERNS as _INFRA_PATTERNS,
    looks_infrastructure as _looks_infrastructure,
)
from state.paths import config_path
from state.plan_manager import PlanManager
from state.schemas import Plan, Task


# Re-exported from :mod:`state.infra_patterns` so the v0.28.0 import
# surface stays stable. v0.29.0 Bug 6 promoted the keyword list to a
# shared module so the schema-load migration shim can consume the same
# heuristic without introducing a cli -> state import cycle.
__all__ = [
    "_INFRA_PATTERNS",
    "_looks_infrastructure",
    "requeue",
]


def _select_task_ids(
    plan: Plan,
    *,
    explicit_tasks: tuple[str, ...],
    phases: tuple[str, ...],
    infrastructure: bool,
    all_blocked: bool,
    capped_phases: bool = False,
) -> tuple[list[str], list[str], list[str]]:
    """Resolve the CLI selectors against ``plan``.

    Returns ``(task_ids_to_requeue, unknown_explicit_ids,
    matched_capped_phase_ids)``. The second tuple is non-empty when the
    operator typo-ed an explicit ``--task`` id; the caller surfaces it
    as exit-1 user-error. The third tuple lists every phase whose
    ``review_status == "capped"`` actually fed into the selection — the
    requeue CLI uses it as the payload for the
    ``requeue.capped_phases_selected`` audit op (always populated when
    ``capped_phases=True``, even if zero blocked tasks rolled up; an
    empty list when ``capped_phases=False``).

    Tasks already at ``status="pending"`` are filtered OUT here (the
    requeue is idempotent — a no-op call writes zero ledger entries).

    v0.38.0 I2 (HK4): ``capped_phases=True`` is the bulk-recovery
    selector that unions every blocked task across phases sitting at
    ``review_status="capped"`` (set by H2's per-phase / I3's plan-scope
    corrective cap). Composes with the other selectors via OR; the
    idempotency filter at the bottom still applies.
    """
    by_id: dict[str, Task] = {}
    blocked_in_phase: dict[str, list[Task]] = {}
    for phase in plan.phases:
        for task in phase.tasks:
            by_id[task.id] = task
            if task.status == "blocked":
                blocked_in_phase.setdefault(phase.id, []).append(task)

    selected: dict[str, Task] = {}
    unknown: list[str] = []

    for tid in explicit_tasks:
        explicit = by_id.get(tid)
        if explicit is None:
            unknown.append(tid)
            continue
        # Explicit ``--task`` selects regardless of current status;
        # the idempotency filter below skips already-pending entries
        # so re-running the command is a true no-op.
        selected[explicit.id] = explicit

    for phase_id in phases:
        for task in blocked_in_phase.get(phase_id, []):
            selected[task.id] = task

    if infrastructure:
        for tasks in blocked_in_phase.values():
            for task in tasks:
                if _looks_infrastructure(task.blocked_reason):
                    selected[task.id] = task

    if all_blocked:
        for tasks in blocked_in_phase.values():
            for task in tasks:
                selected[task.id] = task

    # v0.38.0 I2 (HK4): roll up every blocked task across phases whose
    # ``review_status == "capped"`` (H2 per-phase cap and/or I3 plan-
    # scope cap). The matched phase-id list is returned alongside so
    # the caller can stamp the ``requeue.capped_phases_selected`` audit
    # op with the exact phase set the operator's flag matched — even
    # when zero blocked tasks ultimately fed into the selection (the
    # phase may be capped with all tasks already pending after a prior
    # requeue, in which case the op records a no-op recovery attempt).
    matched_capped_phases: list[str] = []
    if capped_phases:
        for phase in plan.phases:
            if phase.review_status == "capped":
                matched_capped_phases.append(phase.id)
                for task in blocked_in_phase.get(phase.id, []):
                    selected[task.id] = task

    # Idempotency filter: drop tasks that are already pending.
    runnable = [tid for tid, task in selected.items() if task.status != "pending"]
    return runnable, unknown, matched_capped_phases


def _affected_phase_ids(plan: Plan, task_ids: list[str]) -> list[str]:
    """Return the phase ids covered by ``task_ids``, in plan order."""
    by_task: dict[str, str] = {}
    for phase in plan.phases:
        for task in phase.tasks:
            by_task[task.id] = phase.id
    seen: list[str] = []
    for tid in task_ids:
        phase_id = by_task.get(tid)
        if phase_id is not None and phase_id not in seen:
            seen.append(phase_id)
    return seen


def _render_plan_table(
    console: Console, plan: Plan, task_ids: list[str], dry_run: bool
) -> None:
    """Render the per-task action table the operator sees before/after."""
    title = "Requeue (dry-run)" if dry_run else "Requeued"
    table = Table(title=f"{title} ({len(task_ids)} task(s))")
    table.add_column("Task", style="cyan")
    table.add_column("Phase", style="cyan")
    table.add_column("Prior status")
    table.add_column("Reason", overflow="fold")
    by_id_phase: dict[str, tuple[Task, str]] = {}
    for phase in plan.phases:
        for task in phase.tasks:
            by_id_phase[task.id] = (task, phase.id)
    for tid in task_ids:
        task, phase_id = by_id_phase[tid]
        table.add_row(
            tid,
            phase_id,
            task.status,
            task.blocked_reason or "",
        )
    console.print(table)


def _resolve_source_label(
    *,
    explicit_tasks: tuple[str, ...],
    phases: tuple[str, ...],
    infrastructure: bool,
    all_blocked: bool,
    capped_phases: bool = False,
) -> str:
    """Return a short flag-name for the ledger ``source`` field.

    v0.38.0 I2: ``--capped-phases`` slots between ``--all-blocked`` and
    ``--infrastructure`` in the precedence chain. It is broader than
    ``--phase`` / ``--task`` (potentially many phases) but narrower than
    ``--all-blocked`` (skips uncapped phases), so the label reflects
    that it's the bulk-recovery selector for the H2/I3 cap-fire surface.
    """
    if all_blocked:
        return "--all-blocked"
    if capped_phases:
        return "--capped-phases"
    if infrastructure:
        return "--infrastructure"
    if phases:
        return "--phase"
    if explicit_tasks:
        return "--task"
    return "interactive"


@click.command("requeue")
@click.option(
    "--task",
    "tasks",
    multiple=True,
    help="Explicit task id (e.g. '1.1'). May repeat.",
)
@click.option(
    "--phase",
    "phases",
    multiple=True,
    help="Phase id whose blocked tasks should requeue. May repeat.",
)
@click.option(
    "--infrastructure",
    is_flag=True,
    help=(
        "Requeue every blocked task whose ``blocked_reason`` matches "
        "the v0.28.0 infrastructure keyword heuristic (403, 401, "
        "Forbidden, authenticate, ...)."
    ),
)
@click.option(
    "--all-blocked",
    "all_blocked",
    is_flag=True,
    help="Requeue every blocked task in the plan (asks confirmation).",
)
@click.option(
    "--capped-phases",
    "capped_phases",
    is_flag=True,
    default=False,
    help=(
        "Requeue all blocked tasks in phases with review_status='capped' "
        "(set by H2 per-phase / I3 plan-scope corrective-cap fires). "
        "Composes with other selectors via OR."
    ),
)
@click.option(
    "--yes",
    "yes",
    is_flag=True,
    help="Skip the interactive confirmation prompt.",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    help="Show the planned mutations without writing the ledger.",
)
def requeue(
    tasks: tuple[str, ...],
    phases: tuple[str, ...],
    infrastructure: bool,
    all_blocked: bool,
    capped_phases: bool,
    yes: bool,
    dry_run: bool,
) -> None:
    """Flip blocked tasks back to ``pending`` (typed task-status reset)."""
    console = Console()
    cwd = Path.cwd()
    cfg_path = config_path(cwd)
    if not cfg_path.exists():
        console.print(
            f"[red]autodev requeue:[/red] {cfg_path} not found. "
            "Run [bold]autodev init[/bold] first."
        )
        sys.exit(1)
    try:
        load_config(cfg_path)
    except AutodevError as exc:
        console.print(f"[red]autodev requeue: config error[/red]: {exc}")
        sys.exit(1)

    async def _run() -> None:
        pm = PlanManager(cwd, session_id="cli-requeue")
        plan = await pm.load()
        if plan is None:
            console.print(
                "[yellow]autodev requeue:[/yellow] no plan on disk — "
                "nothing to requeue."
            )
            return

        task_ids, unknown, matched_capped_phases = _select_task_ids(
            plan,
            explicit_tasks=tasks,
            phases=phases,
            infrastructure=infrastructure,
            all_blocked=all_blocked,
            capped_phases=capped_phases,
        )
        if unknown:
            joined = ", ".join(sorted(unknown))
            console.print(
                f"[red]autodev requeue:[/red] unknown task id(s): {joined}. "
                "Run [bold]autodev status[/bold] to list valid ids."
            )
            sys.exit(1)

        if not task_ids:
            console.print(
                "[yellow]autodev requeue:[/yellow] nothing to requeue."
            )
            return

        _render_plan_table(console, plan, task_ids, dry_run)

        if dry_run:
            console.print(
                "[cyan]autodev requeue --dry-run:[/cyan] no ledger "
                "entries written."
            )
            return

        if not yes:
            # No selectors + interactive shell: ask for explicit
            # confirmation before mass-flipping.
            confirmed = click.confirm(
                f"Requeue {len(task_ids)} task(s)?", default=False
            )
            if not confirmed:
                console.print("[yellow]autodev requeue:[/yellow] aborted.")
                return

        source = _resolve_source_label(
            explicit_tasks=tasks,
            phases=phases,
            infrastructure=infrastructure,
            all_blocked=all_blocked,
            capped_phases=capped_phases,
        )
        # v0.38.0 I2 (HK4): emit the bulk-recovery audit breadcrumb BEFORE
        # the requeue mutations land so the forensic order of operations
        # reads "operator picked --capped-phases → N tasks requeued"
        # straight off the ledger. Audit-only — the per-task transitions
        # flow through the regular ``update_task_status`` ops emitted
        # inside ``requeue_tasks``. Uses ``pm.ledger_append`` (takes its
        # own lock) so the call is safe against the second lock acquire
        # inside :meth:`PlanManager.requeue_tasks` below.
        if capped_phases:
            await pm.ledger_append(
                op="requeue.capped_phases_selected",
                payload={
                    "phases": list(matched_capped_phases),
                    "task_count": len(task_ids),
                },
            )
        result = await pm.requeue_tasks(
            task_ids,
            reset_phase_review=True,
            source=source,
        )
        console.print(
            f"[green]autodev requeue:[/green] requeued "
            f"{len(result.requeued_task_ids)} task(s); "
            f"reset {len(result.reset_phase_ids)} phase review(s)."
        )

    try:
        asyncio.run(_run())
    except AutodevError as exc:
        console.print(f"[red]autodev requeue failed[/red]: {exc}")
        sys.exit(2)
