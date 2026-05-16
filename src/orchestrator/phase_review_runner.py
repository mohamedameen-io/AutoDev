"""Glue between :class:`~orchestrator.Orchestrator` and the v0.9.0
phase-review tournament engine.

v0.18.0 A2: ``run_multi_branch_phase_review_tournament`` adds branched
phase-review fan-out — N independent reviews run concurrently and a
majority-vote meta-merge produces the final outcome (no LLM synthesis
needed because the phase-review verdict is text-only). Mirrors
:mod:`orchestrator.multi_branch_tournament` but simpler (no synthesizer
step, just text dedup + majority voting).

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
    from config.schema import BranchConfig
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
    *,
    branch_config: "BranchConfig | None" = None,
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
        branch_config: v0.18.0 A1 — optional per-branch config providing
            heterogeneous-model overrides + lane tag. ``None`` (default)
            preserves homogeneous behavior. When set, the runner threads
            ``branch_config.model_overrides`` into the
            :class:`AdapterLLMClient` and suffixes the artifact dir with
            ``-{lane}``.

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

    # v0.25.3: per-tournament auto-disable list. phase_review defaults to
    # ``["opus"]`` (cost guard: one tournament per phase).
    auto_disable = (
        orch.cfg.tournaments.phase_review.auto_disable_for_models or []
    )
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

    # v0.26.0: the v0.25.4 InlineAdapter+tournaments defense-in-depth raise
    # was removed alongside InlineAdapter itself.

    # Load plan to derive spec_hash for the deterministic tournament id.
    plan = await orch.plan_manager.load()
    spec_hash = (plan.spec_hash or "phase-review-stub")[:16].ljust(16, "0") if plan else "phase-review-stub00"
    tournament_id = _phase_review_tournament_id(spec_hash, phase.id)
    # v0.18.0 A1: when a branch_config is supplied, suffix the artifact dir
    # with the lane label so the on-disk layout records each branch's
    # divergent trajectory at a glance.
    if branch_config is not None:
        artifact_dir_name = f"{tournament_id}-{branch_config.lane}"
    else:
        artifact_dir_name = tournament_id
    artifact_dir = autodev_root(orch.cwd) / "tournaments" / artifact_dir_name

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

    # v0.18.0 A1: per-branch role-model overrides. Empty dict / None
    # preserves legacy homogeneous behavior — the global ``model``
    # passed into ``client.call`` is used for every role.
    role_model_overrides: dict[str, str] | None = None
    if branch_config is not None and branch_config.model_overrides:
        role_model_overrides = dict(branch_config.model_overrides)

    client = AdapterLLMClient(
        orch.adapter,
        cwd=orch.cwd,
        role_max_turns=role_max_turns,
        role_allowed_tools=role_allowed_tools,
        role_effort=role_effort,
        role_timeout_s=role_timeout_s,
        role_model_overrides=role_model_overrides,
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

    # v0.16.0: drift-verifier as a final-defense gate. Only fires on
    # A-winners (non-A already failed phase review and produced a
    # corrective_direction). If drift findings are detected, the
    # outcome is flipped to ``accept_phase=False`` with a synthesized
    # corrective_direction so the orchestrator's phase-review handler
    # injects a corrective task. The drift-verifier is opt-in via
    # ``cfg.tournaments.phase_review.drift_verifier_enabled`` (default
    # off for backward compat) — projects opt in once their stub
    # adapters / production registries reliably surface a
    # ``critic_drift_verifier`` response.
    drift_verdict = None
    drift_enabled = getattr(cfg, "drift_verifier_enabled", False)
    if accept_phase and drift_enabled:
        drift_evidence_dir = autodev_root(orch.cwd) / "evidence"
        try:
            from orchestrator.drift_verifier import (
                _CONVERGENCE_SIMILARITY_THRESHOLD,
                run_drift_verifier,
            )

            # v0.34.0 B3: thread the prior corrective diff (if any)
            # so the runner can short-circuit when the new patch is
            # ≥90% identical to the previous one — the corrective loop
            # is not making progress and another dispatch is wasted.
            prior_diff_memo = getattr(orch, "_drift_prior_diff_by_phase", None)
            if prior_diff_memo is None:
                prior_diff_memo = {}
                setattr(orch, "_drift_prior_diff_by_phase", prior_diff_memo)
            attempt_memo = getattr(orch, "_drift_attempt_by_phase", None)
            if attempt_memo is None:
                attempt_memo = {}
                setattr(orch, "_drift_attempt_by_phase", attempt_memo)
            prior_diff = prior_diff_memo.get(phase.id)
            attempt_no = int(attempt_memo.get(phase.id, 0))
            drift_verdict = await run_drift_verifier(
                orch=orch,
                phase=phase,
                evidence_dir=drift_evidence_dir,
                diff_text=diff_text,
                prior_corrective_diff=prior_diff,
                attempt=attempt_no,
            )
            prior_diff_memo[phase.id] = diff_text
            attempt_memo[phase.id] = attempt_no + 1
        except Exception as exc:  # noqa: BLE001
            # Drift-verifier failures must never block phase promotion;
            # log and continue with the tournament's verdict. Telemetry
            # surfaces the error for follow-up.
            logger.warning(
                "drift_verifier.invocation_failed",
                phase_id=phase.id,
                err=str(exc),
            )
            drift_verdict = None

        if drift_verdict is not None and not drift_verdict.passed:
            accept_phase = False
            findings_text = "\n".join(
                f"- {f}" for f in drift_verdict.drift_findings
            ) or "- drift detected (no specific findings parsed)"
            corrective_direction = (
                "Drift verifier detected divergence between the phase spec "
                "and the as-implemented diff:\n" + findings_text
            )
            logger.info(
                "drift_verifier.override_to_corrective",
                phase_id=phase.id,
                n_findings=len(drift_verdict.drift_findings),
            )
            # v0.34.0 B3: convergence-failure ledger breadcrumb so
            # operators can see when the corrective loop self-aborted
            # without parsing the evidence file. Audit-only.
            if getattr(drift_verdict, "convergence_failure", False):
                try:
                    await orch.plan_manager.ledger_append(
                        op="drift_convergence_failure",
                        payload={
                            "task_id": phase.id,
                            "similarity": _CONVERGENCE_SIMILARITY_THRESHOLD,
                            "attempt": int(attempt_memo.get(phase.id, 0)),
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "drift_convergence_failure.ledger_append_failed",
                        phase_id=phase.id,
                        err=str(exc),
                    )

        # v0.16.0: ledger breadcrumb so post-hoc analysis can replay the
        # drift-verifier's verdict without re-reading the evidence file.
        # Audit-only — does NOT mutate plan state. Best-effort: a
        # ledger-append failure must never block phase promotion.
        if drift_verdict is not None:
            try:
                evidence_rel = (
                    str(drift_verdict.evidence_path.relative_to(orch.cwd))
                    if drift_verdict.evidence_path.is_relative_to(orch.cwd)
                    else str(drift_verdict.evidence_path)
                )
                await orch.plan_manager.ledger_append(
                    op="drift_verifier_complete",
                    payload={
                        "phase_id": phase.id,
                        "passed": drift_verdict.passed,
                        "drift_findings": drift_verdict.drift_findings,
                        "evidence_path": evidence_rel,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "drift_verifier.ledger_append_failed",
                    phase_id=phase.id,
                    err=str(exc),
                )

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


async def run_multi_branch_phase_review_tournament(
    orch: "Orchestrator",
    phase: "Phase",
    baseline_commit: str,
    tip_commit: str,
    spec_md: str,
    n_branches: int,
    branch_configs: "list[BranchConfig] | None" = None,
) -> PhaseReviewOutcome:
    """Run N concurrent phase-review tournaments and meta-merge survivors.

    Each branch runs a full phase-review tournament independently (with
    optional :class:`~config.schema.BranchConfig` for hetero-models +
    lane). After all branches return, the meta-merge applies majority
    voting on ``accept_phase`` and concatenate-deduplicates corrective
    text from non-A winners. NO LLM synthesis — phase-review verdicts
    are text-only.

    Survivor floor: ``max(2, ceil(N/2))``. If fewer survive, raises
    :class:`~errors.TournamentError`.

    Args:
        orch: Orchestrator instance.
        phase: Phase being reviewed.
        baseline_commit / tip_commit: Diff range for variant A.
        spec_md: User intent string.
        n_branches: Number of parallel branches (>=1).
        branch_configs: Optional list of per-branch configs; must be of
            length ``n_branches`` if provided.

    Returns:
        :class:`PhaseReviewOutcome` with the meta-merged verdict. The
        ``history`` field carries the union of all branch histories
        (forensics).

    Raises:
        TournamentError: when fewer than the survivor floor of branches
            succeeded.
        ValueError: on caller misuse (n_branches<1 or branch_configs
            length mismatch).
    """
    import asyncio
    import math

    from errors import TournamentError

    if n_branches < 1:
        raise ValueError(f"n_branches must be ≥1, got {n_branches}")
    if branch_configs is not None and len(branch_configs) != n_branches:
        raise ValueError(
            f"len(branch_configs) ({len(branch_configs)}) must equal "
            f"n_branches ({n_branches})"
        )

    # N=1 short-circuit: just call the single-branch path with the lone
    # branch_config (or None) and return its outcome unchanged.
    if n_branches == 1:
        only_bc = branch_configs[0] if branch_configs else None
        return await run_phase_review_tournament(
            orch=orch,
            phase=phase,
            baseline_commit=baseline_commit,
            tip_commit=tip_commit,
            spec_md=spec_md,
            branch_config=only_bc,
        )

    # Audit-trail breadcrumb at start.
    await orch.plan_manager.ledger_append(
        op="multi_branch_phase_review_start",
        payload={
            "phase_id": phase.id,
            "n_branches": n_branches,
            "lanes": [
                (branch_configs[i].lane if branch_configs and branch_configs[i]
                 else None)
                for i in range(n_branches)
            ],
        },
    )

    logger.info(
        "multi_branch_phase_review.start",
        phase_id=phase.id,
        n_branches=n_branches,
    )

    # Step 1: gather N concurrent phase reviews. ``return_exceptions=True``
    # so a single branch failure does NOT cancel siblings.
    coros = [
        run_phase_review_tournament(
            orch=orch,
            phase=phase,
            baseline_commit=baseline_commit,
            tip_commit=tip_commit,
            spec_md=spec_md,
            branch_config=(
                branch_configs[i] if branch_configs is not None else None
            ),
        )
        for i in range(n_branches)
    ]
    raw_results = await asyncio.gather(*coros, return_exceptions=True)

    # Partition into survivors + failures.
    survivors: list[PhaseReviewOutcome] = []
    failures: list[tuple[int, BaseException]] = []
    for i, r in enumerate(raw_results):
        if isinstance(r, BaseException):
            failures.append((i, r))
            logger.warning(
                "multi_branch_phase_review.branch_failed",
                branch_index=i,
                error=str(r),
            )
        else:
            survivors.append(r)

    floor = max(2, math.ceil(n_branches / 2))
    if len(survivors) < floor:
        raise TournamentError(
            f"only {len(survivors)} of {n_branches} phase-review branches "
            f"succeeded; survivor floor is {floor}"
        )

    # Step 2: meta-merge — majority vote on accept_phase + dedup corrective.
    accept_votes = sum(1 for s in survivors if s.accept_phase)
    reject_votes = len(survivors) - accept_votes
    accept_phase_majority = accept_votes >= reject_votes
    # On ties, accept (conservative — incumbent wins).
    if accept_votes == reject_votes:
        accept_phase_majority = True

    # Concatenate-deduplicate corrective text from rejecting survivors.
    corrective_blocks: list[str] = []
    seen: set[str] = set()
    for s in survivors:
        if s.accept_phase or not s.corrective_direction:
            continue
        text = s.corrective_direction.strip()
        # Dedup by line: drop lines we've already seen.
        deduped_lines: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                if deduped_lines and deduped_lines[-1] != "":
                    deduped_lines.append("")
                continue
            if stripped in seen:
                continue
            seen.add(stripped)
            deduped_lines.append(line)
        if deduped_lines:
            corrective_blocks.append("\n".join(deduped_lines).strip())

    merged_corrective: str | None = None
    if not accept_phase_majority and corrective_blocks:
        merged_corrective = "\n\n".join(corrective_blocks)
    elif accept_phase_majority:
        merged_corrective = None

    # Pick a canonical winner label based on the majority verdict.
    if accept_phase_majority:
        meta_winner: WinnerLabelLit = "A"
    else:
        # Use the most common non-A winner label among rejecting survivors.
        non_a_labels = [s.winner for s in survivors if not s.accept_phase]
        if non_a_labels:
            # Mode (first wins on tie).
            counts: dict[str, int] = {}
            for label in non_a_labels:
                counts[label] = counts.get(label, 0) + 1
            meta_winner = max(counts.keys(), key=lambda k: counts[k])  # type: ignore[arg-type,assignment]
        else:
            meta_winner = "B"

    # Concatenate histories from all survivors so post-hoc analysis can
    # walk every pass that ran across the cohort.
    union_history: list[PassResult] = []
    for s in survivors:
        union_history.extend(s.history)

    # Audit-trail breadcrumb at end.
    await orch.plan_manager.ledger_append(
        op="multi_branch_phase_review_meta_merge_complete",
        payload={
            "phase_id": phase.id,
            "n_survivors": len(survivors),
            "n_branches": n_branches,
            "accept_votes": accept_votes,
            "reject_votes": reject_votes,
            "majority_accept": accept_phase_majority,
        },
    )
    await orch.plan_manager.ledger_append(
        op="multi_branch_phase_review_complete",
        payload={
            "phase_id": phase.id,
            "n_branches": n_branches,
            "n_survivors": len(survivors),
            "winner": meta_winner,
            "accept_phase": accept_phase_majority,
        },
    )

    logger.info(
        "multi_branch_phase_review.done",
        phase_id=phase.id,
        n_survivors=len(survivors),
        accept_phase=accept_phase_majority,
    )

    return PhaseReviewOutcome(
        winner=meta_winner,
        accept_phase=accept_phase_majority,
        corrective_direction=merged_corrective,
        history=union_history,
    )


__all__ = [
    "PhaseReviewOutcome",
    "_phase_complexity_rollup",
    "_phase_review_tournament_id",
    "run_multi_branch_phase_review_tournament",
    "run_phase_review_tournament",
]
