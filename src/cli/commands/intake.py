"""``autodev intake`` — run ONLY the intake & clarification phase (ADR-0045).

The opt-in standalone surface (folded alternative #5 in ADR-0045): an operator
runs ``autodev intake "<thin intent>"``, reviews the enriched, locked
``.autodev/spec.md``, then runs ``autodev plan`` against it. The default path
runs intake automatically *inside* ``autodev plan`` (Integration wires that);
this command exists for operators who want to inspect the enriched spec first.

Exit codes:
    0 — intake completed; enriched spec written to ``.autodev/spec.md``.
    1 — config / setup error (missing config, bad load).
    2 — runtime ``AutodevError`` raised inside the orchestrator.
    3 — ADR-0045 ``on_unanswered=fail``: a thin spec had unresolved questions
        and the operator asked for a non-zero exit instead of assumed defaults.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Literal, cast

import click
from rich.console import Console

from adapters.detect import get_adapter
from agents import build_registry
from autologging import get_logger
from config.loader import load_config
from errors import AutodevError
from orchestrator import Orchestrator
from orchestrator.intake_phase import run_intake_phase
from state.paths import config_path


logger = get_logger(__name__)


@click.command("intake")
@click.argument("intent", required=True)
@click.option(
    "--platform",
    type=click.Choice(["claude_code", "cursor", "auto"]),
    default=None,
    help="Override platform selection (else use config + auto-detect).",
)
@click.option(
    "--assume-defaults",
    is_flag=True,
    default=False,
    help=(
        "Headless intake — apply each clarifying question's recommended "
        "default instead of waiting for an operator. This is the headless "
        "default; the flag pins it explicitly."
    ),
)
@click.option(
    "--on-unanswered",
    type=click.Choice(["assume_defaults", "block", "fail"]),
    default=None,
    help=(
        "Override the headless ``on_unanswered`` policy for this run "
        "(else use config; default assume_defaults)."
    ),
)
def intake(
    intent: str,
    platform: str | None,
    assume_defaults: bool,
    on_unanswered: str | None,
) -> None:
    """Run the intake phase only: assess, gather, enrich, clarify, lock spec.md."""
    console = Console()
    cwd = Path.cwd()
    cfg_path = config_path(cwd)
    if not cfg_path.exists():
        console.print(
            f"[red]autodev intake:[/red] {cfg_path} not found. "
            "Run [bold]autodev init[/bold] first."
        )
        sys.exit(1)
    try:
        cfg = load_config(cfg_path)
    except AutodevError as exc:
        console.print(f"[red]autodev intake: config error[/red]: {exc}")
        sys.exit(1)

    # ADR-0045: the standalone surface always runs intake (even if globally
    # disabled in config) — that is the whole point of the command. The
    # ``on_unanswered`` policy is honored so a thin spec can ``fail`` loudly.
    intake_update: dict[str, object] = {"enabled": True}
    if on_unanswered is not None:
        intake_update["on_unanswered"] = on_unanswered
    elif assume_defaults:
        intake_update["on_unanswered"] = "assume_defaults"
    cfg = cfg.model_copy(
        update={"intake": cfg.intake.model_copy(update=intake_update)}
    )

    async def _run() -> int:
        platform_pref = platform or cfg.platform  # type: ignore[assignment]
        adapter, _selection_meta = await get_adapter(
            cast("Literal['claude_code', 'cursor', 'auto']", platform_pref),
            cwd=cwd,
            respect_trigger_context=cfg.adapter_respect_trigger_context,
            cursor_trigger_env_extra=cfg.cursor_trigger_env_extra,
            cfg=cfg,
        )
        registry = build_registry(cfg)
        orch = Orchestrator(cwd=cwd, cfg=cfg, adapter=adapter, registry=registry)
        outcome = await run_intake_phase(orch, intent)

        # on_unanswered=fail: a thin spec that still has unresolved questions
        # exits non-zero (the operator opted out of assumed defaults). We detect
        # this as "gaps existed (not passthrough) and the policy is fail and we
        # produced assumptions"-free output is impossible, so signal on the
        # policy + the presence of a non-passthrough gap path.
        if (
            cfg.intake.on_unanswered == "fail"
            and not outcome.passthrough
            and not outcome.assumptions
            and not outcome.degraded
        ):
            console.print(
                "[red]autodev intake: unresolved questions (on_unanswered=fail)[/red]"
            )
            return 3

        if outcome.passthrough:
            console.print(
                "[green]autodev intake:[/green] spec already well-formed — "
                "locked as-is."
            )
        elif outcome.degraded:
            console.print(
                "[yellow]autodev intake:[/yellow] degraded to the raw intent "
                "(disabled / kill-switch / error). Spec locked unchanged."
            )
        else:
            console.print(
                "[green]autodev intake:[/green] enriched spec locked "
                f"(spec_hash={outcome.spec_hash})."
            )
            if outcome.assumptions:
                console.print("[dim]Assumptions applied (headless defaults):[/dim]")
                for line in outcome.assumptions:
                    console.print(f"  - {line}", style="dim")
        console.print(
            "[dim]Review .autodev/spec.md, then run "
            "[bold]autodev plan[/bold].[/dim]"
        )
        return 0

    try:
        code = asyncio.run(_run())
    except AutodevError as exc:
        console.print(f"[red]autodev intake failed[/red]: {exc}")
        sys.exit(2)
    sys.exit(code)
