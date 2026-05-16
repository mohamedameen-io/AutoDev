"""``autodev status`` — show the current plan and task states."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from config.loader import load_config
from errors import AutodevError
from state.evidence import list_evidence
from state.knowledge import KnowledgeStore
from state.paths import autodev_root, config_path, ledger_path
from state.plan_manager import PlanManager
from state.schemas import Plan, Task


@click.command("status")
@click.option(
    "--blocked",
    is_flag=True,
    help=(
        "Render the structured recovery hints for every blocked task "
        "(v0.32.0 Phase 5 / Gap G). When omitted, a brief blocked-tasks "
        "summary is printed alongside the regular status table whenever "
        "blocked tasks exist."
    ),
)
def status(blocked: bool) -> None:
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
            _print_index_summary(console, cwd, cfg)
            return

        # v0.32.0 (Phase 5, Gap G): when ``--blocked`` is set, render
        # ONLY the blocked-tasks panel and exit — the operator asked
        # specifically for the recovery surface.
        if blocked:
            _render_blocked_section(console, plan, cwd=cwd)
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
        # v0.32.0 (Phase 5, Gap G): when blocked tasks exist, point the
        # operator at the structured recovery surface even on the
        # default status invocation.
        if totals.get("blocked", 0) > 0:
            console.print(
                f"[yellow]{totals['blocked']} blocked task(s).[/yellow] "
                "Run [bold]autodev status --blocked[/bold] for "
                "diagnosis and recovery options."
            )
        _print_knowledge_summary(console, len(swarm_entries), len(hive_entries))
        _print_index_summary(console, cwd, cfg)

    try:
        asyncio.run(_run())
    except AutodevError as exc:
        console.print(f"[red]autodev status failed[/red]: {exc}")
        sys.exit(2)


def _collect_recovery_outcomes(ledger_pth: Path) -> list[dict]:
    """v0.36.0 F3: parse the plan-ledger JSONL and return the F1 ops
    for the most recent run.

    Filters to ``recovery_tier_attempted`` and ``architect_attempt_failed``
    ops. Returns them in append order. Best-effort — malformed ledger
    lines are skipped silently (status is a forensic view; we want it
    to render even when the ledger is partially corrupted).
    """
    if not ledger_pth.exists():
        return []
    rows: list[dict] = []
    try:
        with ledger_pth.open("r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                op = obj.get("op")
                if op in (
                    "recovery_tier_attempted",
                    "architect_attempt_failed",
                    "path_rejection_recorded",
                ):
                    rows.append(obj)
    except OSError:
        return []
    return rows


def _find_architect_dumps(autodev_dir: Path) -> list[Path]:
    """v0.36.0 F3: list ``.autodev/debug/architect-failed-*.md`` paths.

    Sorted by mtime descending so the most recent dump renders first.
    Empty list when the debug dir doesn't exist.
    """
    debug = autodev_dir / "debug"
    if not debug.exists():
        return []
    try:
        dumps = list(debug.glob("architect-failed-*.md"))
    except OSError:
        return []
    dumps.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return dumps


def _render_recovery_outcomes(
    console: Console, rows: list[dict]
) -> None:
    """v0.36.0 F3: render the recovery-tier + architect-attempt sections."""
    tier_rows = [r for r in rows if r.get("op") == "recovery_tier_attempted"]
    attempt_rows = [
        r for r in rows if r.get("op") == "architect_attempt_failed"
    ]
    rejection_rows = [
        r for r in rows if r.get("op") == "path_rejection_recorded"
    ]

    if tier_rows:
        table = Table(title="Recovery Tier Outcomes")
        table.add_column("Tier", justify="right")
        table.add_column("Outcome")
        table.add_column("Reason")
        table.add_column("From → To")
        for r in tier_rows:
            p = r.get("payload", {})
            table.add_row(
                str(p.get("tier", "")),
                p.get("outcome", ""),
                p.get("reason", ""),
                f"{p.get('from_state') or '-'} → {p.get('to_state') or '-'}",
            )
        console.print(table)

    if attempt_rows:
        # Last 3 by ledger order (most recent first).
        recent = attempt_rows[-3:]
        table = Table(title="Architect Attempts (most recent 3)")
        table.add_column("Attempt", justify="right")
        table.add_column("Model")
        table.add_column("Duration (s)", justify="right")
        table.add_column("Rejections", justify="right")
        table.add_column("Primary class")
        for r in recent:
            p = r.get("payload", {})
            table.add_row(
                str(p.get("attempt", "")),
                p.get("model", ""),
                str(p.get("duration_s", "")),
                str(p.get("rejection_count", "")),
                p.get("primary_class", ""),
            )
        console.print(table)

    # Surface the most recent design-class action hint when we saw a
    # rejection. Defers to D1's diagnosis library so the same text the
    # architect-retry envelope renders also lands in the status surface.
    if rejection_rows:
        try:
            from orchestrator.retry_envelope import diagnosis_for_class
        except Exception:  # noqa: BLE001
            diagnosis_for_class = lambda _c: ""  # noqa: E731
        last = rejection_rows[-1].get("payload", {})
        cls = last.get("class", "missing_on_disk")
        action = diagnosis_for_class(cls)
        if action:
            console.print(
                Panel(
                    action,
                    title=f"Action hint (class: {cls})",
                    border_style="cyan",
                )
            )


def _render_blocked_section(
    console: Console,
    plan: Plan,
    cwd: Path | None = None,
) -> None:
    """v0.32.0 (Phase 5, Gap G): render structured recovery panels for
    every blocked task in ``plan``.

    Renders one rich :class:`rich.panel.Panel` per blocked task carrying:

      * the typed block class (from :class:`state.schemas.RecoveryHint.class_`),
      * the ``blocked_reason`` text the orchestrator stamped,
      * discard / pivot counters from the task metadata when present,
      * the recommended user action (single sentence),
      * relevant evidence + debug paths,
      * commands the operator can copy-paste, rendered as Syntax blocks.

    When no blocked tasks exist, prints a green confirmation. Tasks
    without a populated ``recovery_hint`` (legacy v0.31.x blocks) are
    still rendered with the available metadata so the user sees
    *something* actionable; the helper degrades gracefully rather than
    silently dropping the row.
    """
    blocked_tasks: list[Task] = [
        t for phase in plan.phases for t in phase.tasks if t.status == "blocked"
    ]
    if not blocked_tasks:
        console.print("[green]No blocked tasks.[/green]")
        return

    console.print(
        f"[bold yellow]Blocked tasks ({len(blocked_tasks)}):[/bold yellow]"
    )
    for task in blocked_tasks:
        # Locate the parent phase so the panel header carries it.
        phase_id = task.phase_id
        header = f"Task {task.id} | {task.title} | Phase {phase_id}"

        body_lines: list[str] = []
        hint = task.recovery_hint
        if hint is not None:
            body_lines.append(f"[bold]Block class:[/bold] {hint.class_}")
        elif task.block_reason_class is not None:
            body_lines.append(
                f"[bold]Block class:[/bold] {task.block_reason_class} "
                "[dim](legacy — no structured hint)[/dim]"
            )
        else:
            body_lines.append(
                "[bold]Block class:[/bold] [dim]unknown[/dim]"
            )

        if task.blocked_reason:
            body_lines.append(f"[bold]Reason:[/bold] {task.blocked_reason}")

        # Surface stuck-ladder counters when the orchestrator stamped
        # them (v0.15+). Counters live on Task.metadata for blocked
        # tasks that traversed the escalation ladder.
        discard_count = task.metadata.get("discard_count")
        pivot_count = task.metadata.get("pivot_count")
        if discard_count is not None or pivot_count is not None:
            body_lines.append(
                f"[bold]Discard count:[/bold] {discard_count or 0}  "
                f"[bold]Pivot count:[/bold] {pivot_count or 0}"
            )

        if hint is not None:
            body_lines.append(
                f"[bold]Recommended action:[/bold] {hint.recommended_user_action}"
            )
            if hint.relevant_evidence_files:
                body_lines.append("[bold]Evidence files:[/bold]")
                for path in hint.relevant_evidence_files:
                    body_lines.append(f"  - {path}")
            if hint.relevant_debug_files:
                body_lines.append("[bold]Debug files:[/bold]")
                for path in hint.relevant_debug_files:
                    body_lines.append(f"  - {path}")

        body_text = "\n".join(body_lines)
        panel = Panel(body_text, title=header, border_style="yellow")
        console.print(panel)

        # Commands rendered as a Syntax block underneath each panel
        # for copy-paste convenience.
        if hint is not None and hint.commands_to_try:
            commands_text = "\n".join(hint.commands_to_try)
            console.print(
                Syntax(
                    commands_text,
                    "bash",
                    theme="ansi_dark",
                    line_numbers=False,
                )
            )
        console.print("")

    # v0.36.0 F3: surface recovery-tier outcomes, recent architect
    # attempts, archived rejected-plan dumps, and the most recent
    # design-class action hint. Skipped when ``cwd`` is None (back-
    # compat for callers that haven't migrated to the new signature).
    if cwd is not None:
        try:
            rows = _collect_recovery_outcomes(ledger_path(cwd))
        except Exception:  # noqa: BLE001
            rows = []
        if rows:
            _render_recovery_outcomes(console, rows)
        try:
            dumps = _find_architect_dumps(autodev_root(cwd))
        except Exception:  # noqa: BLE001
            dumps = []
        if dumps:
            console.print("[bold]Archived Rejected Plans[/bold]")
            for path in dumps:
                console.print(f"  - {path}")


def _print_knowledge_summary(console: Console, swarm_count: int, hive_count: int) -> None:
    """Render the Knowledge section. Purely informational — never changes exit code."""
    console.print(
        f"[cyan]Knowledge:[/cyan] {swarm_count} lessons in swarm tier, "
        f"{hive_count} in hive tier"
    )


def _print_index_summary(console: Console, cwd: Path, cfg) -> None:
    """v0.25.0: one-line file/symbol index summary. Purely informational —
    swallows query errors and reports a friendly state when missing."""
    if not getattr(cfg, "index_enabled", False):
        return
    db_path = cwd / cfg.index_path
    if not db_path.exists():
        console.print(
            "[cyan]Index:[/cyan] [yellow]MISSING[/yellow] "
            "(run [bold]autodev init --rebuild-index[/bold])"
        )
        return
    try:
        from state.file_index import IndexQuery

        meta = IndexQuery(db_path).meta_summary()
    except Exception as exc:  # noqa: BLE001 - never let index break status
        console.print(
            f"[cyan]Index:[/cyan] [red]error[/red] ({exc})"
        )
        return
    file_count = meta.get("file_count", "?")
    symbol_count = meta.get("symbol_count", "?")
    last_indexed = meta.get("last_indexed_at", "?")
    console.print(
        f"[cyan]Index:[/cyan] {file_count} files, {symbol_count} symbols "
        f"(last indexed {last_indexed})"
    )
