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
from cli._blocked_banner import _maybe_print_blocked_banner
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
    workers = getattr(cfg, "index_build_workers", 0)
    batch_size = getattr(cfg, "index_build_batch_size", 1000)
    try:
        from state.file_index import IndexBuilder, _last_indexed_sha

        if not db_path.exists():
            IndexBuilder.build_full(
                cwd, db_path, workers=workers, batch_size=batch_size
            )
        else:
            IndexBuilder.build_incremental(
                cwd,
                db_path,
                since_sha=_last_indexed_sha(db_path),
                workers=workers,
                batch_size=batch_size,
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

    # v0.28.0 (Bug 10): mandatory preflight probe. ``get_adapter`` already
    # runs ``healthcheck`` inside ``detect_platform``, but we re-probe here
    # because the user typically invokes ``autodev resume`` after just
    # fixing auth — a cached/stale negative would lock them out. The
    # probe must succeed before we enter the execute loop.
    preflight_failure: tuple[str, str] | None = None

    async def _run() -> None:
        # v0.26.0: the inline suspend-state branch (read
        # ``.autodev/inline-state.json``, instantiate InlineAdapter, gate on
        # ``has_pending_response``) was removed alongside InlineAdapter.
        # The migrator in ``config.schema`` rewrites legacy
        # ``platform: inline`` to ``platform: claude_code`` on load so
        # ``cfg.platform`` is always one of {claude_code, cursor, auto}.
        nonlocal preflight_failure
        platform_pref = platform or cfg.platform  # type: ignore[assignment]
        adapter_pair = await get_adapter(
            cast("Literal['claude_code', 'cursor', 'auto']", platform_pref),
            cwd=cwd,
            respect_trigger_context=cfg.adapter_respect_trigger_context,
            cursor_trigger_env_extra=cfg.cursor_trigger_env_extra,
            cfg=cfg,
        )
        adapter: PlatformAdapter = adapter_pair[0]
        selection_meta = adapter_pair[1]

        # Mandatory re-probe — NOT cached. If ``get_adapter`` succeeded but
        # the underlying CLI / auth has flipped between then and now, we
        # surface that here rather than thrashing the orchestrator.
        ok, details = await adapter.healthcheck()
        if not ok:
            preflight_failure = ("resume", details)
            return

        # v0.32.0 (Phase 5, Gap G): inform the operator before the
        # orchestrator runs that a previous session left blocked
        # tasks behind.
        await _maybe_print_blocked_banner(console, cwd)

        registry = build_registry(cfg)
        orch = Orchestrator(cwd=cwd, cfg=cfg, adapter=adapter, registry=registry)
        # v0.38.0 HK10: see plan.py for rationale. Best-effort breadcrumb.
        try:
            await orch.plan_manager.ledger_append(
                op="adapter_selected", payload=selection_meta
            )
        except Exception:  # noqa: BLE001 — forensics, not correctness
            pass
        tasks = await orch.resume()
        _render_resume_summary(console, tasks)

    try:
        asyncio.run(_run())
    except AutodevError as exc:
        console.print(f"[red]autodev resume failed[/red]: {exc}")
        sys.exit(2)

    if preflight_failure is not None:
        _, reason = preflight_failure
        _print_preflight_failure(console, "resume", reason)
        sys.exit(2)


def _print_preflight_failure(console: Console, command: str, reason: str) -> None:
    """Render the actionable infrastructure-not-ready block (Bug 10).

    Format mirrors the multi-line layout used by ``reset.py`` / ``status.py``
    for operator-facing diagnostics.
    """
    console.print(f"[red]autodev {command}: infrastructure not ready[/red]")
    console.print(f"  reason: {reason}")
    console.print("")
    console.print("  Refresh your auth and retry:")
    console.print(
        "    • If using API key: verify [bold]ANTHROPIC_API_KEY[/bold]"
    )
    console.print(
        "    • If using corp proxy: refresh "
        "[bold]ANTHROPIC_AUTH_TOKEN[/bold]"
    )
    console.print(
        "    • If using subscription: run [bold]claude /login[/bold]"
    )


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
