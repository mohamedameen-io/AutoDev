"""v0.32.0 (Phase 5, Gap G): pre-flight blocked-task banner helper.

Shared by ``autodev plan``, ``autodev execute``, and ``autodev resume``
so every entry point that kicks off the orchestrator first informs the
operator that a previous run left blocked tasks behind.

The banner is purely informational — it never aborts execution. The
intent is to nudge the user toward ``autodev status --blocked`` for the
structured recovery surface introduced in the same phase.

Failures must NOT abort the calling command: a corrupt plan / missing
ledger / Pydantic validation error reading the plan should leave the
banner silent. The CLI then continues exactly as it did pre-v0.32.0.
"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console


async def _maybe_print_blocked_banner(console: Console, cwd: Path) -> None:
    """Render a yellow banner when the on-disk plan has blocked tasks.

    The function is async because :class:`PlanManager` exposes the plan
    via ``await pm.load()``. Callers should ``await`` this helper near
    the top of their command's ``_run`` coroutine — before the first
    ``orch.execute()`` / ``orch.plan()`` / ``orch.resume()`` call — so
    the banner lands above any orchestrator output.

    Best-effort: any exception while loading the plan is swallowed and
    the banner is skipped. The plan / ledger paths are owned by other
    entry points whose own error reporting is the right surface for
    those failures.
    """
    try:
        from state.plan_manager import PlanManager  # noqa: PLC0415

        pm = PlanManager(cwd, session_id="blocked-banner-readonly")
        plan = await pm.load()
    except Exception:  # noqa: BLE001 - banner is informational
        return
    if plan is None:
        return
    blocked_count = sum(
        1 for phase in plan.phases for task in phase.tasks if task.status == "blocked"
    )
    if blocked_count == 0:
        return
    plural = "s" if blocked_count != 1 else ""
    console.print(
        f"[yellow]⚠️ {blocked_count} task{plural} are blocked. "
        "Run [bold]autodev status --blocked[/bold] for diagnosis "
        "and recovery options.[/yellow]"
    )


__all__ = ["_maybe_print_blocked_banner"]
