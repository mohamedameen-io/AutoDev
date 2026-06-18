"""``autodev plan`` — run the PLAN phase end-to-end.

Exit codes:
    0 — plan approved and persisted.
    1 — config / setup error (missing config, bad load).
    2 — runtime ``AutodevError`` raised inside the orchestrator.
    4 — v0.36.0 G1: spec rejected by :func:`validate_spec_text` before
        dispatch (under-specified intent — see printed reasons).
    5 — v0.36.0 F2: structured ``NetworkProbeFailure`` from the
        adapter healthcheck (probe retried + still failing).
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from typing import Literal, cast

from adapters.detect import get_adapter
from adapters.fitness import compute_fitness_score, get_fitness_warning
from agents import build_registry
from autologging import get_logger
from cli._blocked_banner import _maybe_print_blocked_banner
from config.loader import load_config
from errors import AutodevError
from orchestrator import Orchestrator
from orchestrator.spec_validator import validate_spec_text
from state.paths import config_path, index_db_path


logger = get_logger(__name__)


def _maybe_refresh_index(cwd: Path, cfg) -> None:
    """v0.25.0: incremental refresh hook (mirrors execute.py).

    Skips on missing ``cfg.index_enabled`` or while ``.autodev/index.db.building``
    marker exists. Builds full when missing, incremental otherwise. Failures
    are logged + swallowed so the planner can continue with a stale index.
    """
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


@click.command("plan")
@click.argument("intent", required=True)
@click.option(
    "--platform",
    type=click.Choice(["claude_code", "cursor", "auto"]),
    default=None,
    help="Override platform selection (else use config + auto-detect).",
)
@click.option(
    "--complexity",
    type=click.Choice(["low", "medium", "high", "max"]),
    default=None,
    help=(
        "Override task complexity for this run (else use config). Drives the "
        "architect's effort floor (xhigh for {low,medium,high}, max for max)."
    ),
)
@click.option(
    "--skip-spec-validation",
    is_flag=True,
    default=False,
    help=(
        "v0.36.0 G1: bypass the front-gate spec validator. Use only when "
        "dispatching a deliberately laconic spec (the architect has the "
        "context it needs from elsewhere)."
    ),
)
@click.option(
    "--no-intake",
    is_flag=True,
    default=False,
    help=(
        "ADR-0045: disable the intake & clarification phase for this run "
        "(plan against the raw intent, skip gather/enrich/clarify)."
    ),
)
@click.option(
    "--assume-defaults",
    is_flag=True,
    default=False,
    help=(
        "ADR-0045: headless intake — apply each clarifying question's "
        "recommended default instead of waiting for an operator (never "
        "hangs in CI/cron). This is already the headless default."
    ),
)
def plan(
    intent: str,
    platform: str | None,
    complexity: str | None,
    skip_spec_validation: bool,
    no_intake: bool,
    assume_defaults: bool,
) -> None:
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

    if complexity is not None:
        cfg = cfg.model_copy(update={"user_complexity": complexity})

    # ADR-0045: intake-phase flag plumbing. ``--no-intake`` disables the phase;
    # ``--assume-defaults`` pins the headless ``on_unanswered`` policy. Integration
    # wires ``run_intake_phase`` into ``run_plan_phase``; here we only set the
    # config the phase reads. ``model_copy`` on the nested model keeps the parent
    # immutable-by-copy semantics consistent with the complexity override above.
    intake_enabled = cfg.intake.enabled and not no_intake
    intake_update: dict[str, object] = {"enabled": intake_enabled}
    if assume_defaults:
        intake_update["on_unanswered"] = "assume_defaults"
    cfg = cfg.model_copy(update={"intake": cfg.intake.model_copy(update=intake_update)})

    # v0.36.0 G1: cheap front-gate. Reject obviously under-specified
    # intents before paying for explorer + domain_expert + architect.
    # The validator inspects the intent text directly (intent is what
    # ``run_plan_phase`` later writes to ``spec.md``); a path-based
    # variant lives in :func:`orchestrator.spec_validator.validate_spec`
    # for callers that hand in a file.
    #
    # ADR-0045: when intake is ENABLED it RESOLVES under-specification
    # (gather → enrich → clarify), so the hard G1 reject is bypassed —
    # the thin spec is the input intake exists to enrich, not to bounce.
    # The deterministic gate still runs when intake is off / skipped.
    if not skip_spec_validation and not intake_enabled:
        result = validate_spec_text(intent)
        if not result.ok:
            console.print(
                "[red]autodev plan: spec rejected by validator[/red]"
            )
            for reason in result.reasons:
                console.print(f"  - {reason}", style="red")
            console.print(
                "[yellow]Pass --skip-spec-validation to bypass.[/yellow]"
            )
            # Best-effort ledger emission. We may not have an
            # initialised plan-manager yet; write directly via
            # :func:`state.ledger.append_entry`.
            try:
                from state.ledger import append_entry
                import uuid

                asyncio.run(
                    append_entry(
                        cwd,
                        op="spec_validation_failed",
                        payload={
                            "path": str(intent[:200]),
                            "reasons": list(result.reasons),
                        },
                        session_id=str(uuid.uuid4()),
                    )
                )
            except Exception as exc:  # noqa: BLE001 - never block exit on ledger I/O
                logger.warning("plan.spec_validation_ledger_failed", err=str(exc))
            sys.exit(4)

    # v0.25.0: incremental file/symbol index refresh before Orchestrator
    # construction. The planner queries the index for candidate files
    # to inject into the architect's envelope.
    _maybe_refresh_index(cwd, cfg)

    async def _run() -> None:
        # v0.26.0: ``platform: inline`` is auto-migrated to ``claude_code``
        # by the schema validator; ``cfg.platform`` is always one of
        # {claude_code, cursor, auto} here.
        platform_pref = platform or cfg.platform  # type: ignore[assignment]
        adapter, selection_meta = await get_adapter(
            cast("Literal['claude_code', 'cursor', 'auto']", platform_pref),
            cwd=cwd,
            respect_trigger_context=cfg.adapter_respect_trigger_context,
            cursor_trigger_env_extra=cfg.cursor_trigger_env_extra,
            cfg=cfg,
        )
        # v0.31.0 (Phase 5.4): emit a fitness telemetry line + warn on
        # poor adapter/codebase fit. Best-effort.
        _emit_fitness_signal(console, cwd, adapter)
        # v0.32.0 (Phase 5, Gap G): inform the operator before the
        # planner runs that a previous session left blocked tasks
        # behind.
        await _maybe_print_blocked_banner(console, cwd)
        registry = build_registry(cfg)
        orch = Orchestrator(cwd=cwd, cfg=cfg, adapter=adapter, registry=registry)
        # v0.38.0 HK10: forensic breadcrumb so post-mortems can
        # correlate "which selection arm fired this boot" with
        # downstream behaviour. Best-effort — never block the planner
        # on a ledger I/O error.
        try:
            await orch.plan_manager.ledger_append(
                op="adapter_selected", payload=selection_meta
            )
        except Exception:  # noqa: BLE001 — forensics, not correctness
            pass
        # Phase 0 (cost/time telemetry): record a run-start watermark
        # (ledger high-water seq) + monotonic clock so we can attribute
        # exactly this run's ``invocation_cost`` ops + wall time afterward.
        from state.run_summary import (
            append_run_summary,
            current_ledger_seq,
            sum_invocation_cost,
        )

        run_t0 = time.monotonic()
        start_seq = current_ledger_seq(cwd)
        approved = await orch.plan(intent)
        _render_plan_summary(console, approved)
        # Best-effort run summary — a telemetry failure NEVER fails the run.
        try:
            elapsed_s = time.monotonic() - run_t0
            cost_usd = sum_invocation_cost(cwd, after_seq=start_seq)
            n_tasks = sum(len(p.tasks) for p in approved.phases)
            append_run_summary(
                cwd,
                phase="plan",
                cost_usd=cost_usd,
                elapsed_s=elapsed_s,
                tasks=n_tasks,
            )
            console.print(
                f"[dim]Run cost: ${cost_usd:.4f} USD · wall {elapsed_s:.0f}s[/dim]"
            )
        except Exception as exc:  # noqa: BLE001 — telemetry only
            logger.warning("plan.run_summary_failed", err=str(exc))

    try:
        asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 - branch on type below
        # v0.36.0 F2: structured probe failure caught here; renders
        # the typed ``.suggestion`` and exits with a dedicated code.
        # Imported lazily so adapters-package import failures don't
        # mask the much-more-common AutodevError path.
        try:
            from adapters.base import NetworkProbeFailure
        except Exception:  # noqa: BLE001
            NetworkProbeFailure = ()  # type: ignore[misc,assignment]
        if NetworkProbeFailure and isinstance(exc, NetworkProbeFailure):  # type: ignore[truthy-function]  # guards the () import-fallback; intentional
            console.print(
                f"[red]autodev plan: network probe failed[/red] "
                f"({exc.adapter}, {exc.attempts} attempts)"
            )
            console.print(f"  last_error: {exc.last_error}")
            if exc.suggestion:
                console.print(f"  [yellow]suggestion:[/yellow] {exc.suggestion}")
            sys.exit(5)
        if isinstance(exc, AutodevError):
            console.print(f"[red]autodev plan failed[/red]: {exc}")
            sys.exit(2)
        raise


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


def _emit_fitness_signal(console: Console, cwd: Path, adapter) -> None:
    """v0.31.0 (Phase 5.4): score the adapter against the codebase.

    Mirrors the helper in ``cli.commands.execute``. Prints a yellow
    warning when score < threshold; always logs structured telemetry.
    Best-effort: a profile/score failure never blocks planning.
    """
    try:
        from runtime.language_profile import compute_language_profile
        from autologging import get_logger

        profile = compute_language_profile(cwd)
        adapter_name = type(adapter).__name__.replace("Adapter", "").lower()
        if adapter_name == "claudecode":
            adapter_name = "claude_code"
        score = compute_fitness_score(adapter_name, profile)
        warn = get_fitness_warning(adapter_name, profile)
        get_logger(component="adapter.fitness").info(
            "adapter.fitness",
            adapter=adapter_name,
            score=score,
            profile=profile,
        )
        if warn is not None:
            console.print(f"[yellow]{warn}[/yellow]")
    except Exception:  # noqa: BLE001 - signal is informational
        pass
