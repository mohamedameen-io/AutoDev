"""Glue between :class:`~orchestrator.Orchestrator` and the v0.9.0
phase-review tournament engine.

Mirrors :mod:`orchestrator.impl_tournament_runner` shape but uses
:class:`tournament.phase_review.PhaseReviewBundle` as the content type
and :class:`tournament.core.Tournament` (the base class) directly — no
worktree materialization is needed because B / AB winners produce
direction text, not code. The orchestrator parses the direction text
into corrective sub-tasks via
:func:`orchestrator.corrective_parser.parse_corrective_direction` after
this runner returns.

Responsibilities:
    - Build a :class:`PhaseReviewBundle` from the phase's
      ``baseline_commit..tip_commit`` git diff and acceptance criteria.
    - Resolve the effective tournament model and honor
      ``cfg.tournaments.auto_disable_for_models``.
    - Run the tournament to convergence (or ``max_rounds``).
    - Decide ``PhaseReviewOutcome``: A → ``accept_phase=True``;
      B / AB → ``accept_phase=False, corrective_direction=<text>``.
    - Write :class:`~state.schemas.TournamentEvidence` with
      ``phase="phase_review"``.
    - Append a ``phase_review_complete`` ledger breadcrumb.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from adapters import InlineAdapter
from adapters.git_utils import _git_diff_range, extract_files_from_diff
from autologging import get_logger
from runtime.resource_probe import probe_host, resolve_parallelism
from state.evidence import write_evidence
from state.paths import autodev_root
from state.schemas import TournamentEvidence
from tournament import (
    AdapterLLMClient,
    PassResult,
    PhaseReviewBundle,
    Tournament,
    TournamentConfig,
    _PhaseReviewContentHandler,
)
from tournament.effort import resolve_role_effort
from tournament.timeouts import resolve_role_timeout_s


if TYPE_CHECKING:
    from orchestrator import Orchestrator
    from state.schemas import Phase


logger = get_logger(__name__)


# Same role mix as plan / impl tournaments.
_TOURNAMENT_ROLES: tuple[str, ...] = (
    "critic_t",
    "architect_b",
    "synthesizer",
    "judge",
)


WinnerLabelLit = Literal["A", "B", "AB"]


@dataclass
class PhaseReviewOutcome:
    """Result of a phase-review tournament.

    Attributes:
        winner: Final tournament winner — ``"A"`` (incumbent), ``"B"`` or
            ``"AB"`` (corrective direction).
        accept_phase: ``True`` iff the winner is ``"A"``. The orchestrator
            uses this to set ``Phase.review_status = "accepted"`` directly,
            without injecting any corrective tasks.
        corrective_direction: For ``"B"`` / ``"AB"`` winners, the
            architect_b / synthesizer's corrective bullet list. ``None``
            when ``accept_phase`` is ``True`` or when the tournament is
            auto-disabled.
        history: Per-pass results (preserved for evidence and CLI
            inspection).
    """

    winner: WinnerLabelLit
    accept_phase: bool
    corrective_direction: str | None
    history: list[PassResult]


def _phase_review_tournament_id(spec_hash: str, phase_id: str) -> str:
    """Derive a deterministic tournament id for a phase review.

    Centralised so the runner and the CLI ``tournament phase-review``
    subcommand can both compute the on-disk artifact path without drift.
    """
    safe_phase = phase_id.replace("/", "_").replace(" ", "_")
    return f"phase-review-{spec_hash[:8]}-{safe_phase}"


def _resolve_tournament_model(orch: "Orchestrator") -> str | None:
    """Return the judge model (or ``None`` if unresolved)."""
    spec = orch.registry.get("judge")
    if spec is not None and spec.model:
        return spec.model
    agent_cfg = orch.cfg.agents.get("judge")
    if agent_cfg is not None and agent_cfg.model:
        return agent_cfg.model
    return None


def _is_auto_disabled(model: str | None, auto_disable: list[str]) -> bool:
    if not model or not auto_disable:
        return False
    low = model.lower()
    return any(marker.lower() in low for marker in auto_disable)


def _phase_complexity_rollup(phase: "Phase") -> str | None:
    """Roll the phase's complexity up from its tasks.

    Per the user-locked-in design, no new ``Phase.complexity`` field
    exists — we compute the bucket inline as
    ``max(t.complexity for t in phase.tasks if t.complexity)``. Returns
    ``None`` when no task has a complexity tag (matches the legacy fallback
    in :func:`tournament.effort.resolve_role_effort`).
    """
    order = {"simple": 0, "medium": 1, "complex": 2}
    best: str | None = None
    best_score = -1
    for t in phase.tasks:
        c = getattr(t, "complexity", None)
        if not isinstance(c, str):
            continue
        score = order.get(c, -1)
        if score > best_score:
            best_score = score
            best = c
    return best


def _build_role_overrides(
    orch: "Orchestrator", plan_complexity: str | None
) -> tuple[
    dict[str, int],
    dict[str, list[str] | None],
    dict[str, int],
    dict[str, str],
]:
    """Build per-role tournament overrides keyed on the rollup complexity."""
    role_max_turns: dict[str, int] = {}
    role_allowed_tools: dict[str, list[str] | None] = {}
    role_timeout_s: dict[str, int] = {}
    role_effort: dict[str, str] = {}

    for role in _TOURNAMENT_ROLES:
        spec = orch.registry.get(role)
        if spec is None:
            continue
        role_max_turns[role] = spec.max_turns or 1
        role_allowed_tools[role] = list(spec.tools) if spec.tools else []
        timeout_s = resolve_role_timeout_s(role, plan_complexity)
        if timeout_s is not None:
            role_timeout_s[role] = timeout_s
        effort = resolve_role_effort(
            role,
            orch.cfg.agents.get(role),
            plan_complexity,
            orch.cfg.user_complexity,
        )
        if effort is not None:
            role_effort[role] = effort
    return role_max_turns, role_allowed_tools, role_timeout_s, role_effort


def _summarize_tasks(phase: "Phase") -> str:
    """Produce a one-line summary of task statuses for the bundle."""
    counts: dict[str, int] = {}
    for t in phase.tasks:
        counts[t.status] = counts.get(t.status, 0) + 1
    parts = [f"{n} {s}" for s, n in sorted(counts.items())]
    return f"{len(phase.tasks)} tasks total: " + ", ".join(parts)


async def run_phase_review_tournament(
    orch: "Orchestrator",
    phase: "Phase",
    baseline_commit: str,
    tip_commit: str,
    spec_md: str,
) -> PhaseReviewOutcome:
    """Run the phase-review tournament and return the outcome.

    Args:
        orch: Orchestrator instance carrying adapter / config / plugin
            registry / plan_manager.
        phase: The phase being reviewed. Its acceptance criteria, tasks,
            and corrective_task_ids are read here.
        baseline_commit: HEAD sha at phase entry. Together with
            ``tip_commit`` this is the diff range materialized into the
            A variant of the bundle.
        tip_commit: HEAD sha at phase completion (typically the current
            HEAD).
        spec_md: Original spec markdown — used as the tournament's
            ``task_prompt``.

    Returns:
        :class:`PhaseReviewOutcome`. On A win → ``accept_phase=True,
        corrective_direction=None``. On B / AB win → ``accept_phase=False,
        corrective_direction=<direction text>``. On auto-disable → A-win
        no-op outcome.
    """
    cfg = orch.cfg.tournaments.phase_review
    if not cfg.enabled:
        logger.info("phase_review_tournament.disabled", phase_id=phase.id)
        return PhaseReviewOutcome(
            winner="A",
            accept_phase=True,
            corrective_direction=None,
            history=[],
        )

    auto_disable = orch.cfg.tournaments.auto_disable_for_models
    model = _resolve_tournament_model(orch)
    if _is_auto_disabled(model, auto_disable):
        logger.info(
            "phase_review_tournament.auto_disabled",
            model=model,
            auto_disable_for_models=auto_disable,
        )
        return PhaseReviewOutcome(
            winner="A",
            accept_phase=True,
            corrective_direction=None,
            history=[],
        )

    assert not isinstance(orch.adapter, InlineAdapter), (
        "Tournament runners must use subprocess adapters, not InlineAdapter"
    )

    # Load plan to derive spec_hash for the deterministic tournament id.
    plan = await orch.plan_manager.load()
    spec_hash = (plan.spec_hash or "phase-review-stub")[:16].ljust(16, "0") if plan else "phase-review-stub00"
    tournament_id = _phase_review_tournament_id(spec_hash, phase.id)
    artifact_dir = autodev_root(orch.cwd) / "tournaments" / tournament_id

    # Build the as-implemented diff for the A variant.
    diff_text = _git_diff_range(orch.cwd, baseline_commit, tip_commit) or ""
    files_changed = _extract_files_from_diff(diff_text)

    plan_complexity = _phase_complexity_rollup(phase)

    # Initial bundle (variant A — incumbent).
    initial_bundle = PhaseReviewBundle(
        phase_id=phase.id,
        phase_title=phase.title,
        baseline_commit=baseline_commit,
        tip_commit=tip_commit,
        diff=diff_text,
        files_changed=files_changed,
        acceptance=list(phase.acceptance),
        task_summary=_summarize_tasks(phase),
        test_summary=None,
        variant_label="A",
    )

    role_max_turns, role_allowed_tools, role_timeout_s, role_effort = (
        _build_role_overrides(orch, plan_complexity)
    )
    client = AdapterLLMClient(
        orch.adapter,
        cwd=orch.cwd,
        role_max_turns=role_max_turns,
        role_allowed_tools=role_allowed_tools,
        role_effort=role_effort,
        role_timeout_s=role_timeout_s,
    )

    # v0.10.0: resolve subprocess parallelism via the runtime probe.
    # See :mod:`orchestrator.plan_tournament_runner` for the rationale.
    # Phase-review tournaments default to a 3-judge cohort so the resolver
    # typically lands on min(3, ...) on dev hardware.
    resolved_parallelism = resolve_parallelism(
        configured=orch.cfg.tournaments.max_parallel_subprocesses,
        capacity=probe_host(),
        role_mix="phase_review",
        num_judges=cfg.num_judges,
    )
    tcfg = TournamentConfig(
        num_judges=cfg.num_judges,
        convergence_k=cfg.convergence_k,
        max_rounds=cfg.max_rounds,
        model=model,
        max_parallel_subprocesses=resolved_parallelism,
        score_stability_window=cfg.score_stability_window,
        score_stability_max_delta=cfg.score_stability_max_delta,
        winner_stability_window=cfg.winner_stability_window,
        max_plan_lines_growth_ratio=cfg.max_plan_lines_growth_ratio,
    )

    # Seed RNG so judge presentation order is reproducible across re-runs
    # of the SAME phase review (ledger-replay friendliness).
    seed_input = (spec_hash[:8] + phase.id).encode("utf-8")
    rng_seed = int.from_bytes(seed_input, "big") & ((1 << 64) - 1)
    rng = random.Random(rng_seed)

    judge_plugins = (
        list(orch.plugin_registry.judges.values())
        if orch.plugin_registry is not None
        else []
    )
    tournament = Tournament(
        handler=_PhaseReviewContentHandler(),
        client=client,
        cfg=tcfg,
        artifact_dir=artifact_dir,
        rng=rng,
        judge_plugins=judge_plugins,
    )

    logger.info(
        "phase_review_tournament.start",
        tournament_id=tournament_id,
        phase_id=phase.id,
        model=model,
        num_judges=tcfg.num_judges,
        convergence_k=tcfg.convergence_k,
        max_rounds=tcfg.max_rounds,
    )

    final_bundle, history = await tournament.run(
        task_prompt=spec_md, initial=initial_bundle
    )

    winner_label: WinnerLabelLit = (
        history[-1].meta.get("effective_winner", history[-1].winner)
        if history
        else "A"
    )  # type: ignore[assignment]
    if winner_label not in ("A", "B", "AB"):
        winner_label = "A"

    accept_phase = winner_label == "A"
    corrective_direction: str | None = None
    if not accept_phase and final_bundle.direction_text:
        corrective_direction = final_bundle.direction_text

    converged = bool(history) and (
        len(history) < tcfg.max_rounds
        or (
            history[-1].winner == "A"
            and sum(1 for h in reversed(history) if h.winner == "A")
            >= tcfg.convergence_k
        )
    )

    logger.info(
        "phase_review_tournament.done",
        tournament_id=tournament_id,
        phase_id=phase.id,
        winner=winner_label,
        accept_phase=accept_phase,
        passes=len(history),
        artifact_dir=str(artifact_dir),
    )

    # Write evidence keyed by a synthetic "phase-{id}" task_id so
    # downstream evidence-listing code finds it without conflating with
    # task-level evidence.
    ev = TournamentEvidence(
        task_id=f"phase-{phase.id}",
        tournament_id=tournament_id,
        phase="phase_review",
        passes=len(history),
        winner=winner_label,
        converged=converged,
        history=[h.model_dump(mode="json") for h in history],
        final_diff=final_bundle.diff or None,
    )
    await write_evidence(orch.cwd, f"phase-{phase.id}", ev)

    # Audit-only ledger breadcrumb. Plan mutations (review_status,
    # corrective tasks) are emitted by the orchestrator after this
    # runner returns — keeping the runner free of plan-state side-effects
    # mirrors the impl tournament's separation of concerns.
    await orch.plan_manager.ledger_append(
        op="phase_review_complete",
        payload={
            "tournament_id": tournament_id,
            "phase_id": phase.id,
            "passes": len(history),
            "winner": winner_label,
            "accept_phase": accept_phase,
            "artifact_dir": str(artifact_dir.relative_to(orch.cwd))
            if artifact_dir.is_relative_to(orch.cwd)
            else str(artifact_dir),
        },
    )

    return PhaseReviewOutcome(
        winner=winner_label,
        accept_phase=accept_phase,
        corrective_direction=corrective_direction,
        history=history,
    )


# v0.13.0: lifted to ``adapters.git_utils.extract_files_from_diff`` so the
# secretscan diff-scope path can reuse it. The legacy private name is kept
# as an alias for any callers (test fixtures, etc.) that still reference
# it from this module.
_extract_files_from_diff = extract_files_from_diff


__all__ = [
    "PhaseReviewOutcome",
    "_phase_complexity_rollup",
    "_phase_review_tournament_id",
    "run_phase_review_tournament",
]
