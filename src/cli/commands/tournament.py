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

from agents import resolve_claude_tools
from config.defaults import default_config
from config.loader import load_config
from config.schema import AutodevConfig
from errors import AutodevError
from orchestrator.plan_parser import extract_complexity
from state.paths import autodev_root, config_path
from tournament import (
    AdapterLLMClient,
    PlanContentHandler,
    Tournament,
    TournamentConfig,
)
from tournament.effort import resolve_role_effort
from tournament.timeouts import resolve_role_timeout_s


_TOURNAMENT_ROLES_FOR_CLI: tuple[str, ...] = (
    "critic_t",
    "architect_b",
    "synthesizer",
    "judge",
)


def _cli_role_overrides(
    cfg: AutodevConfig,
    markdown: str | None = None,
) -> tuple[
    dict[str, int],
    dict[str, list[str] | None],
    dict[str, int],
    dict[str, str],
]:
    """Build tournament-role overrides from an AutodevConfig (no registry).

    Mirrors the orchestrator helpers in ``plan_tournament_runner`` /
    ``impl_tournament_runner``. Used by the standalone CLI which has access
    to ``cfg`` but does not build a full ``Orchestrator`` registry.

    When ``markdown`` is supplied (typically the loaded ``--input`` plan),
    the architect's ``COMPLEXITY:`` line is extracted from it and used to
    derive per-role effort via the matrix and per-role timeout_s via the
    timeout table. Without markdown (or with markdown that lacks the
    directive), only explicit per-role ``agent_cfg.effort`` overrides take
    effect — non-architect roles fall through to None.

    Returns
        ``(role_max_turns, role_allowed_tools, role_timeout_s, role_effort)``.
        ``role_timeout_s`` was added in v0.5.4.
    """
    role_max_turns: dict[str, int] = {}
    role_allowed_tools: dict[str, list[str] | None] = {}
    role_timeout_s: dict[str, int] = {}
    role_effort: dict[str, str] = {}

    plan_complexity: str | None = (
        extract_complexity(markdown) if markdown is not None else None
    )

    for role in _TOURNAMENT_ROLES_FOR_CLI:
        agent_cfg = cfg.agents.get(role)
        if agent_cfg is None:
            continue
        role_max_turns[role] = agent_cfg.max_turns or 1
        tools = resolve_claude_tools(role)
        role_allowed_tools[role] = list(tools) if tools else []
        timeout_s = resolve_role_timeout_s(role, plan_complexity)
        if timeout_s is not None:
            role_timeout_s[role] = timeout_s
        effort = resolve_role_effort(
            role, agent_cfg, plan_complexity, cfg.user_complexity
        )
        if effort is not None:
            role_effort[role] = effort
    return role_max_turns, role_allowed_tools, role_timeout_s, role_effort


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


def _run_command_options(fn):
    """Apply the existing ``run`` options. Used by both the legacy flat-flag
    invocation and the explicit ``tournament run`` subcommand so the two
    paths share the same option surface (and option order).
    """
    fn = click.option(
        "--files",
        "files_changed",
        type=str,
        default=None,
        help="Comma-separated list of changed files for --phase=impl.",
    )(fn)
    fn = click.option(
        "--task-id",
        "task_id",
        type=str,
        default="cli-impl",
        help="Task ID for --phase=impl (default: cli-impl).",
    )(fn)
    fn = click.option(
        "--task-desc",
        "task_desc",
        type=str,
        default=None,
        help="Task description for --phase=impl.",
    )(fn)
    fn = click.option(
        "--input-diff",
        "input_diff",
        type=click.Path(exists=False, dir_okay=False, path_type=Path),
        default=None,
        help="Unified diff file for --phase=impl (alternative to --input).",
    )(fn)
    fn = click.option(
        "--max-rounds",
        type=int,
        default=None,
        help="Override tournaments.*.max_rounds for this run.",
    )(fn)
    fn = click.option(
        "--dry-run",
        is_flag=True,
        help="Skip LLM calls; use canned responses.",
    )(fn)
    fn = click.option(
        "--input",
        "input_path",
        type=click.Path(exists=False, dir_okay=False, path_type=Path),
        default=None,
        help=(
            "Input file (required for --phase=plan; optional diff file for "
            "--phase=impl)."
        ),
    )(fn)
    fn = click.option(
        "--phase",
        type=click.Choice(["plan", "impl"], case_sensitive=False),
        required=True,
        help="Which tournament variant to run.",
    )(fn)
    return fn


def _dispatch_run(
    phase: str,
    input_path: Path | None,
    dry_run: bool,
    max_rounds: int | None,
    input_diff: Path | None,
    task_desc: str | None,
    task_id: str,
    files_changed: str | None,
) -> None:
    """Shared body for both the legacy flat-flag invocation and ``tournament run``.

    Extracted so the same logic services
    ``autodev tournament --phase=plan ...`` (preserved for backward compat)
    AND the new ``autodev tournament run --phase=plan ...`` form.
    """
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


# ---------------------------------------------------------------------------
# v0.6.0: Click group + subcommands ('run' and 'promote'), with backward
# compatibility for the legacy flat-flag form ``tournament --phase=plan ...``.
# ---------------------------------------------------------------------------


_LEGACY_KNOWN_FLAGS = {
    "--phase",
    "--input",
    "--input-diff",
    "--dry-run",
    "--max-rounds",
    "--task-desc",
    "--task-id",
    "--files",
    "-h",
    "--help",
}


class _TournamentGroup(click.Group):
    """Custom group that forwards bare ``tournament --phase=...`` invocations
    to the ``run`` subcommand, preserving backward compatibility with the
    pre-v0.6.0 flat-flag CLI surface.

    Click groups normally consume the first positional arg as the
    subcommand name. We override :meth:`resolve_command` to inject ``run``
    when the first arg looks like a flag-form invocation.
    """

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        # Insert 'run' before the args if the first arg is one of the
        # legacy flat flags (rather than a known subcommand). This keeps
        # the legacy form ``tournament --phase=plan ...`` working while
        # still accepting ``tournament run ...`` and ``tournament promote ...``.
        if args:
            first = args[0]
            # Strip any '=value' suffix to compare against the flag name.
            first_flag = first.split("=", 1)[0]
            known_subs = set(self.commands.keys())
            if first_flag in _LEGACY_KNOWN_FLAGS and first_flag not in known_subs:
                args = ["run", *args]
        return super().parse_args(ctx, args)


@click.group(
    "tournament",
    cls=_TournamentGroup,
    invoke_without_command=False,
)
def tournament() -> None:
    """Run, salvage, and inspect plan/impl self-refinement tournaments.

    Subcommands:

      \b
      run      Run a plan or implementation tournament against a file (the
               legacy flat-flag form ``tournament --phase=plan ...`` still
               works as a synonym).
      promote  Salvage an incumbent from a previously-run tournament's
               on-disk artifacts and write it to the local plan.json.
    """


@tournament.command("run")
@_run_command_options
def run_subcommand(
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
    _dispatch_run(
        phase=phase,
        input_path=input_path,
        dry_run=dry_run,
        max_rounds=max_rounds,
        input_diff=input_diff,
        task_desc=task_desc,
        task_id=task_id,
        files_changed=files_changed,
    )


@tournament.command("promote")
@click.option(
    "--tournament-id",
    "tournament_id",
    required=True,
    type=str,
    help="Tournament directory name under .autodev/tournaments/ (e.g. plan-b536bfe8).",
)
@click.option(
    "--pass",
    "pass_num",
    type=int,
    default=None,
    help="Promote a specific pass; defaults to the latest incumbent.",
)
def promote_subcommand(tournament_id: str, pass_num: int | None) -> None:
    """Salvage a tournament's incumbent into the local plan.json.

    Reads ``.autodev/tournaments/<tournament-id>/incumbent_after_NN.md``
    (latest by default, or the explicit pass number from ``--pass``),
    parses it as a Plan, and writes it to ``.autodev/plan.json`` via the
    PlanManager. This is the manual fallback for the automatic
    on-tournament-error recovery in ``run_plan_phase``.
    """
    from orchestrator.plan_parser import parse_plan_markdown
    from state.plan_manager import PlanManager
    from tournament.state import TournamentArtifactStore

    console = Console()
    cwd = Path.cwd()
    artifact_dir = autodev_root(cwd) / "tournaments" / tournament_id
    if not artifact_dir.exists():
        console.print(
            f"[red]autodev tournament promote:[/red] "
            f"tournament dir not found: {artifact_dir}"
        )
        sys.exit(2)

    store = TournamentArtifactStore(artifact_dir)
    if pass_num is not None:
        recovered = store.read_incumbent_at(pass_num)
        if recovered is None:
            console.print(
                f"[red]autodev tournament promote:[/red] "
                f"no incumbent_after_{pass_num:02d}.md in {artifact_dir}"
            )
            sys.exit(2)
        used_pass: int | None = pass_num
    else:
        recovered = store.latest_incumbent_md()
        used_pass = store.latest_incumbent_pass_num()
        if recovered is None:
            console.print(
                f"[red]autodev tournament promote:[/red] "
                f"no incumbent files (or initial_a.md) in {artifact_dir}"
            )
            sys.exit(2)

    # Parse the recovered markdown using a deterministic stub spec_hash.
    # (Plan-tournament salvage runs are by definition orphaned from their
    # original spec; the spec_hash here is a placeholder that lets the Plan
    # validator pass without forcing the user to re-derive the original hash.)
    spec_hash_stub = f"salvage-{tournament_id}"[:16].ljust(16, "0")
    try:
        plan = parse_plan_markdown(recovered, spec_hash=spec_hash_stub)
    except Exception as exc:  # noqa: BLE001
        console.print(
            f"[red]autodev tournament promote:[/red] "
            f"failed to parse recovered markdown: {exc}"
        )
        sys.exit(2)

    async def _persist() -> None:
        pm = PlanManager(cwd, session_id="cli-tournament-promote")
        await pm.init_plan(plan)

    try:
        asyncio.run(_persist())
    except AutodevError as exc:
        console.print(f"[red]autodev tournament promote failed:[/red] {exc}")
        sys.exit(2)

    pass_label = used_pass if used_pass is not None else "(initial)"
    console.print(
        f"[green]Promoted incumbent[/green] from "
        f"[cyan]{tournament_id}[/cyan] pass={pass_label} "
        f"({len(recovered)} bytes) to .autodev/plan.json"
    )


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
        # Pass the loaded plan markdown so _cli_role_overrides can extract
        # the architect's COMPLEXITY: classification and resolve per-role
        # effort + timeout_s accordingly.
        rmt, rat, rts, ref = _cli_role_overrides(cfg, markdown)
        client = AdapterLLMClient(
            adapter,
            cwd=cwd,
            role_max_turns=rmt,
            role_allowed_tools=rat,
            role_effort=ref,
            role_timeout_s=rts,
        )
        judge_cfg = cfg.agents.get("judge")
        model = judge_cfg.model if judge_cfg else None

    tcfg = TournamentConfig(
        num_judges=plan_cfg.num_judges,
        convergence_k=plan_cfg.convergence_k,
        max_rounds=effective_max_rounds,
        model=model,
        max_parallel_subprocesses=cfg.tournaments.max_parallel_subprocesses,
        score_stability_window=plan_cfg.score_stability_window,
        score_stability_max_delta=plan_cfg.score_stability_max_delta,
        winner_stability_window=plan_cfg.winner_stability_window,
        max_plan_lines_growth_ratio=plan_cfg.max_plan_lines_growth_ratio,
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
        rmt, rat, rts, ref = _cli_role_overrides(cfg)
        client = AdapterLLMClient(
            adapter,
            cwd=cwd,
            role_max_turns=rmt,
            role_allowed_tools=rat,
            role_effort=ref,
            role_timeout_s=rts,
        )
        judge_cfg = cfg.agents.get("judge")
        model = judge_cfg.model if judge_cfg else None

    tcfg = TournamentConfig(
        num_judges=impl_cfg.num_judges,
        convergence_k=impl_cfg.convergence_k,
        max_rounds=effective_max_rounds,
        model=model,
        max_parallel_subprocesses=cfg.tournaments.max_parallel_subprocesses,
        score_stability_window=impl_cfg.score_stability_window,
        score_stability_max_delta=impl_cfg.score_stability_max_delta,
        winner_stability_window=impl_cfg.winner_stability_window,
        # Impl tournaments default to ``None`` (line-ratio is plan-only);
        # plumbed through for symmetry.
        max_plan_lines_growth_ratio=impl_cfg.max_plan_lines_growth_ratio,
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
