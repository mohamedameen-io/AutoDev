"""``autodev tournament`` — standalone tournament runner.

Phase 6 implements ``--phase=plan`` end-to-end against a markdown file
input. ``--phase=impl`` is reserved for Phase 7 and prints
"not yet implemented".

Flow for ``--phase=plan``:

  1. Load ``.autodev/config.json`` from ``cwd`` (or default config in
     ``--dry-run`` mode if no project exists).
  2. Read ``--input`` as the initial plan markdown (version A).
  3. Resolve a task prompt:
        - ``<input>.spec.md`` if it exists, else
        - the first ``# ...`` heading in the markdown, else
        - ``"refine this plan"``.
  4. Build an adapter + :class:`AdapterLLMClient`, or use a
     :class:`DryRunLLMClient` when ``--dry-run``.
  5. Run the :class:`Tournament` with :class:`PlanContentHandler`.
  6. Print a per-pass table + summary; artifacts live under
     ``.autodev/tournaments/plan-<id>/``.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from config.defaults import default_config
from config.loader import load_config
from errors import AutodevError
from state.paths import autodev_root, config_path
from tournament import (
    AdapterLLMClient,
    PlanContentHandler,
    Tournament,
    TournamentConfig,
)


# ---------------------------------------------------------------------------
# DryRunLLMClient — canned responses so the tournament can execute offline.
# ---------------------------------------------------------------------------


class DryRunLLMClient:
    """Deterministic offline client used by ``--dry-run``.

    Returns role-specific canned text. The judge always produces a parseable
    ``RANKING: 1, 2, 3`` so Borda aggregates to ``A`` (position 1 ⇒ slot 1 ⇒
    whichever label the shuffle put at position 1). With conservative
    tie-break toward A the tournament converges at ``convergence_k`` rounds.
    """

    async def call(
        self,
        *,
        system: str,
        user: str,
        role: str,
        model: str | None = None,
    ) -> str:
        if role == "critic_t":
            return "DRY-RUN critic: no substantive issues identified."
        if role == "architect_b":
            # Echo the incumbent so the revision is a no-op semantically.
            return _extract_incumbent_from_prompt(user) or user
        if role == "synthesizer":
            return _extract_first_version(user) or user
        if role == "judge":
            return "DRY-RUN judge.\n\nRANKING: 1, 2, 3"
        return "DRY-RUN default response."


def _extract_incumbent_from_prompt(prompt: str) -> str | None:
    """Pull the CURRENT PROPOSAL block out of the architect_b prompt, if present."""
    marker = "CURRENT PROPOSAL:\n---\n"
    idx = prompt.find(marker)
    if idx < 0:
        return None
    start = idx + len(marker)
    end = prompt.find("\n---\n", start)
    if end < 0:
        return None
    return prompt[start:end]


def _extract_first_version(prompt: str) -> str | None:
    """Pull VERSION X from the synthesizer prompt."""
    marker = "VERSION X:\n---\n"
    idx = prompt.find(marker)
    if idx < 0:
        return None
    start = idx + len(marker)
    end = prompt.find("\n---\n", start)
    if end < 0:
        return None
    return prompt[start:end]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _derive_task_prompt(input_path: Path, markdown: str) -> str:
    """Find a suitable task prompt for the tournament.

    Precedence:
      1. ``<input_path>.spec.md`` sibling file.
      2. First ``# ...`` heading in the markdown.
      3. Fallback string.
    """
    spec = input_path.with_suffix(input_path.suffix + ".spec.md")
    if not spec.exists():
        # Also try replacing the suffix directly (plan.md -> plan.spec.md).
        alt = input_path.with_name(input_path.stem + ".spec.md")
        if alt.exists():
            spec = alt
    if spec.exists():
        return spec.read_text(encoding="utf-8").strip()

    for line in markdown.splitlines():
        s = line.strip()
        if s.startswith("# ") and len(s) > 2:
            return s[2:].strip()
    return "Refine this plan."


def _render_history_table(console: Console, history: list) -> None:
    table = Table(title="Tournament passes")
    table.add_column("Pass", style="cyan", justify="right")
    table.add_column("Winner", style="magenta")
    table.add_column("Scores")
    table.add_column("Valid judges", justify="right")
    table.add_column("Elapsed (s)", justify="right")
    for h in history:
        scores = ", ".join(f"{k}={v}" for k, v in sorted(h.scores.items()))
        table.add_row(
            str(h.pass_num),
            h.winner,
            scores,
            str(h.valid_judges),
            f"{h.elapsed_s:.2f}",
        )
    console.print(table)


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


@click.command("tournament")
@click.option(
    "--phase",
    type=click.Choice(["plan", "impl"], case_sensitive=False),
    required=True,
    help="Which tournament variant to run.",
)
@click.option(
    "--input",
    "input_path",
    type=click.Path(exists=False, dir_okay=False, path_type=Path),
    default=None,
    help="Input file (required for --phase=plan; optional diff file for --phase=impl).",
)
@click.option("--dry-run", is_flag=True, help="Skip LLM calls; use canned responses.")
@click.option(
    "--max-rounds",
    type=int,
    default=None,
    help="Override tournaments.*.max_rounds for this run.",
)
@click.option(
    "--input-diff",
    "input_diff",
    type=click.Path(exists=False, dir_okay=False, path_type=Path),
    default=None,
    help="Unified diff file for --phase=impl (alternative to --input).",
)
@click.option(
    "--task-desc",
    "task_desc",
    type=str,
    default=None,
    help="Task description for --phase=impl.",
)
@click.option(
    "--task-id",
    "task_id",
    type=str,
    default="cli-impl",
    help="Task ID for --phase=impl (default: cli-impl).",
)
@click.option(
    "--files",
    "files_changed",
    type=str,
    default=None,
    help="Comma-separated list of changed files for --phase=impl.",
)
def tournament(
    phase: str,
    input_path: Path | None,
    dry_run: bool,
    max_rounds: int | None,
    input_diff: Path | None,
    task_desc: str | None,
    task_id: str,
    files_changed: str | None,
) -> None:
    """Run a plan or implementation tournament against a file."""
    console = Console()
    phase_lower = phase.lower()

    if phase_lower == "impl":
        # Resolve diff source: --input-diff takes precedence over --input.
        diff_path = input_diff or input_path
        if diff_path is None:
            console.print(
                "[red]autodev tournament --phase=impl:[/red] "
                "--input-diff (or --input) is required."
            )
            sys.exit(2)
        if not diff_path.exists():
            console.print(
                f"[red]autodev tournament:[/red] diff file not found: {diff_path}"
            )
            sys.exit(2)
        try:
            asyncio.run(
                _run_impl_tournament_cli(
                    console=console,
                    diff_path=diff_path,
                    task_desc=task_desc
                    or f"Refine implementation from {diff_path.name}",
                    task_id=task_id,
                    files_changed=[
                        f.strip() for f in files_changed.split(",") if f.strip()
                    ]
                    if files_changed
                    else [],
                    dry_run=dry_run,
                    max_rounds_override=max_rounds,
                )
            )
        except AutodevError as exc:
            console.print(f"[red]autodev tournament failed:[/red] {exc}")
            sys.exit(2)
        return

    if phase_lower != "plan":
        # Unreachable via click.Choice, but keep the branch defensive.
        console.print(f"[red]unknown phase:[/red] {phase!r}")
        sys.exit(1)

    if input_path is None:
        console.print(
            "[red]autodev tournament --phase=plan:[/red] --input is required."
        )
        sys.exit(2)

    if not input_path.exists():
        console.print(f"[red]autodev tournament:[/red] input not found: {input_path}")
        sys.exit(2)

    try:
        asyncio.run(
            _run_plan_tournament_cli(
                console=console,
                input_path=input_path,
                dry_run=dry_run,
                max_rounds_override=max_rounds,
            )
        )
    except AutodevError as exc:
        console.print(f"[red]autodev tournament failed:[/red] {exc}")
        sys.exit(2)


async def _run_plan_tournament_cli(
    *,
    console: Console,
    input_path: Path,
    dry_run: bool,
    max_rounds_override: int | None,
) -> None:
    """Standalone plan-tournament runner used by ``autodev tournament``."""
    cwd = Path.cwd()
    cfg_path = config_path(cwd)

    if cfg_path.exists():
        cfg = load_config(cfg_path)
    elif dry_run:
        # Dry-run mode tolerates an uninitialized project — useful for
        # experimenting with a plan markdown outside a real repo.
        cfg = default_config()
        console.print(
            "[yellow]No .autodev/config.json found; using defaults (--dry-run).[/yellow]"
        )
    else:
        console.print(
            f"[red]autodev tournament:[/red] {cfg_path} not found. "
            "Run [bold]autodev init[/bold] first, or pass --dry-run."
        )
        sys.exit(1)

    markdown = input_path.read_text(encoding="utf-8")
    task_prompt = _derive_task_prompt(input_path, markdown)

    plan_cfg = cfg.tournaments.plan
    effective_max_rounds = max_rounds_override or plan_cfg.max_rounds

    tournament_id = f"plan-{uuid.uuid4().hex[:8]}"
    artifact_dir = autodev_root(cwd) / "tournaments" / tournament_id

    # Build client: dry-run OR real adapter.
    client: Any
    if dry_run:
        client = DryRunLLMClient()
        model: str | None = "dry-run"
    else:
        # Deferred import: adapter module pulls in httpx/subprocess code we
        # don't want to load during --dry-run or in tests.
        from adapters.detect import get_adapter

        adapter = await get_adapter(cfg.platform)
        client = AdapterLLMClient(adapter, cwd=cwd)
        judge_cfg = cfg.agents.get("judge")
        model = judge_cfg.model if judge_cfg else None

    tcfg = TournamentConfig(
        num_judges=plan_cfg.num_judges,
        convergence_k=plan_cfg.convergence_k,
        max_rounds=effective_max_rounds,
        model=model,
        max_parallel_subprocesses=cfg.tournaments.max_parallel_subprocesses,
    )

    console.print(
        f"[bold cyan]autodev tournament --phase=plan[/bold cyan] "
        f"id={tournament_id} rounds<= {effective_max_rounds} "
        f"judges={plan_cfg.num_judges} k={plan_cfg.convergence_k} "
        f"{'[dry-run]' if dry_run else ''}"
    )
    console.print(f"[dim]Input:[/dim] {input_path}")
    console.print(f"[dim]Artifacts:[/dim] {artifact_dir}")

    tour = Tournament(
        handler=PlanContentHandler(),
        client=client,
        cfg=tcfg,
        artifact_dir=artifact_dir,
    )
    final_md, history = await tour.run(task_prompt=task_prompt, initial=markdown)

    _render_history_table(console, history)
    console.print(
        f"[green]Tournament complete.[/green] passes={len(history)} "
        f"final_winner={history[-1].winner if history else 'n/a'}"
    )
    console.print(f"[green]Final output:[/green] {artifact_dir / 'final_output.md'}")
    # Last line: a short indicator for scripts.
    console.print(f"final_bytes={len(final_md)}")


async def _run_impl_tournament_cli(
    *,
    console: Console,
    diff_path: Path,
    task_desc: str,
    task_id: str,
    files_changed: list[str],
    dry_run: bool,
    max_rounds_override: int | None,
) -> None:
    """Standalone impl-tournament runner used by ``autodev tournament --phase=impl``."""
    from tournament import (
        ImplBundle,
        ImplContentHandler,
        ImplTournament,
    )

    cwd = Path.cwd()
    cfg_path = config_path(cwd)

    if cfg_path.exists():
        cfg = load_config(cfg_path)
    elif dry_run:
        cfg = default_config()
        console.print(
            "[yellow]No .autodev/config.json found; using defaults (--dry-run).[/yellow]"
        )
    else:
        console.print(
            f"[red]autodev tournament:[/red] {cfg_path} not found. "
            "Run [bold]autodev init[/bold] first, or pass --dry-run."
        )
        import sys

        sys.exit(1)

    diff_text = diff_path.read_text(encoding="utf-8")
    impl_cfg = cfg.tournaments.impl
    effective_max_rounds = max_rounds_override or impl_cfg.max_rounds

    tournament_id = f"impl-{uuid.uuid4().hex[:8]}"
    artifact_dir = autodev_root(cwd) / "tournaments" / tournament_id

    initial_bundle = ImplBundle(
        task_id=task_id,
        task_description=task_desc,
        diff=diff_text,
        files_changed=files_changed,
    )

    # Build client: dry-run OR real adapter.
    client: Any
    if dry_run:
        client = _DryRunImplLLMClient()
        model: str | None = "dry-run"
    else:
        from adapters.detect import get_adapter

        adapter = await get_adapter(cfg.platform)
        client = AdapterLLMClient(adapter, cwd=cwd)
        judge_cfg = cfg.agents.get("judge")
        model = judge_cfg.model if judge_cfg else None

    tcfg = TournamentConfig(
        num_judges=impl_cfg.num_judges,
        convergence_k=impl_cfg.convergence_k,
        max_rounds=effective_max_rounds,
        model=model,
        max_parallel_subprocesses=cfg.tournaments.max_parallel_subprocesses,
    )

    console.print(
        f"[bold cyan]autodev tournament --phase=impl[/bold cyan] "
        f"id={tournament_id} rounds<={effective_max_rounds} "
        f"judges={impl_cfg.num_judges} k={impl_cfg.convergence_k} "
        f"{'[dry-run]' if dry_run else ''}"
    )
    console.print(f"[dim]Diff:[/dim] {diff_path}")
    console.print(f"[dim]Artifacts:[/dim] {artifact_dir}")

    # For CLI standalone mode, use a no-op worktree manager (no real git ops).
    class _NoopWorktreeManager:
        async def create(self, label: str, base_ref: str = "HEAD") -> Path:
            wt = artifact_dir / "worktrees" / label
            wt.mkdir(parents=True, exist_ok=True)
            return wt

        async def cleanup_all(self) -> None:
            pass

    class _NoopCoderRunner:
        async def run(
            self,
            variant_label: str,
            direction: str,
            worktree: Path,
            task: ImplBundle,
        ) -> ImplBundle:
            # In dry-run CLI mode, return a bundle carrying the direction text.
            return ImplBundle(
                task_id=task.task_id,
                task_description=task.task_description,
                diff=task.diff,
                files_changed=task.files_changed,
                tests_passed=task.tests_passed,
                tests_failed=task.tests_failed,
                tests_total=task.tests_total,
                test_output_excerpt=f"[dry-run variant {variant_label}]",
                variant_label=variant_label,  # type: ignore[arg-type]
                notes=direction,
            )

    tour = ImplTournament(
        handler=ImplContentHandler(),
        client=client,
        cfg=tcfg,
        artifact_dir=artifact_dir,
        coder_runner=_NoopCoderRunner(),
        worktree_manager=_NoopWorktreeManager(),
    )
    final_bundle, history = await tour.run(
        task_prompt=task_desc, initial=initial_bundle
    )

    _render_history_table(console, history)
    console.print(
        f"[green]Tournament complete.[/green] passes={len(history)} "
        f"final_winner={history[-1].winner if history else 'n/a'}"
    )
    console.print(f"[green]Artifacts:[/green] {artifact_dir}")
    console.print(f"final_diff_bytes={len(final_bundle.diff or '')}")


class _DryRunImplLLMClient:
    """Deterministic offline client for impl tournament ``--dry-run``."""

    async def call(
        self,
        *,
        system: str,
        user: str,
        role: str,
        model: str | None = None,
    ) -> str:
        if role == "critic_t":
            return "DRY-RUN critic: no substantive issues identified."
        if role == "architect_b":
            return "- Keep the existing approach\n- No changes needed"
        if role == "synthesizer":
            return "- Synthesize: keep version X approach"
        if role == "judge":
            return "DRY-RUN judge.\n\nRANKING: 1, 2, 3"
        return "DRY-RUN default response."
