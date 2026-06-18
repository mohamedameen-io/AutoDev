"""Glue between :class:`~orchestrator.Orchestrator` and the Phase-5
tournament engine for plan-markdown refinement.

Kept separate from :mod:`orchestrator.plan_phase` so the FSM file
stays focused on the plan flow and this module owns tournament wiring.

Responsibilities:
    - Resolve the effective model for tournament roles and honor
      ``cfg.tournaments.auto_disable_for_models``.
    - Build the :class:`~tournament.llm.AdapterLLMClient` over the
      orchestrator's adapter.
    - Construct :class:`~tournament.core.Tournament` with
      :class:`~tournament.plan_tournament.PlanContentHandler` and run
      it against the draft plan markdown.
    - Append a ``plan_tournament_complete`` ledger breadcrumb so ``resume``
      can detect "plan phase already tournamented".
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

from autologging import get_logger
from errors import TournamentError
from orchestrator.huge_repo_overrides import resolve_huge_repo_parallelism
from orchestrator.plan_parser import extract_complexity
from runtime.resource_probe import probe_host, resolve_parallelism
from state.knowledge import TournamentEvent
from state.paths import autodev_root
from tournament import (
    AdapterLLMClient,
    PlanContentHandler,
    Tournament,
    TournamentConfig,
)
from tournament.effort import resolve_role_effort
from tournament.llm import _TEXT_ONLY_NO_TOOL_ROLES
from tournament.timeouts import resolve_role_timeout_s


if TYPE_CHECKING:
    from config.schema import BranchConfig
    from orchestrator import Orchestrator


logger = get_logger(__name__)


# Tournament roles are called in this order each pass; the judge model is the
# most consequential because it drives the Borda aggregation. We resolve the
# judge role as the "representative" model for the auto-disable check.
_TOURNAMENT_ROLES: tuple[str, ...] = ("critic_t", "architect_b", "synthesizer", "judge")


def _plan_tournament_id(spec_hash: str, branch_index: int | None = None) -> str:
    """Derive the deterministic plan-tournament id from a spec hash.

    Centralised so :mod:`orchestrator.plan_phase` (which reads the salvage
    artifacts on a tournament failure) and the runner itself can never drift
    in their notion of "where this tournament lives on disk."

    Args:
        spec_hash: 16-hex-char digest of the user spec.
        branch_index: Optional branch index for v0.12.0 multi-branch
            tournaments. When ``None`` (default), returns the legacy
            single-branch id ``f"plan-{spec_hash[:8]}"``. When set,
            returns the branch-namespaced id
            ``f"plan-{spec_hash[:8]}-branch{N}"``. Used to construct a
            distinct ``tournament_id`` per branch so resume state and
            ledger ops can be correlated.

    Note: the on-disk ``artifact_dir`` for a multi-branch run uses the
    ``tournaments/multi-{spec_hash[:8]}/branch-N/`` layout (see
    :func:`run_plan_tournament`); the ``tournament_id`` returned here is
    used as a logging / ledger correlation key, NOT as the directory
    name. They differ deliberately: the parent ``multi-{hash}/`` dir
    keeps branch artifacts grouped, while the per-branch id stays a
    flat string for breadcrumb compatibility.
    """
    if branch_index is None:
        return f"plan-{spec_hash[:8]}"
    return f"plan-{spec_hash[:8]}-branch{branch_index}"


def _build_role_overrides(
    orch: "Orchestrator",
    plan_complexity: str | None,
) -> tuple[
    dict[str, int],
    dict[str, list[str] | None],
    dict[str, int],
    dict[str, str],
]:
    """Build per-role ``max_turns``, ``allowed_tools``, ``timeout_s`` and ``effort`` maps.

    ``plan_complexity`` is supplied by the caller — at plan-tournament time the
    parsed Plan has not yet been persisted to ``plan_manager`` (that happens
    *after* the tournament refines the markdown), so reading via
    ``plan_manager.load()`` would always return None. :func:`run_plan_tournament`
    extracts the value directly from the architect's markdown via
    :func:`orchestrator.plan_parser.extract_complexity`.

    Reads each tournament role's :class:`~adapters.types.AgentSpec` from
    ``orch.registry``. Roles missing from the registry are simply omitted (the
    client falls back to its defaults). ``role_effort`` is computed via
    :func:`tournament.effort.resolve_role_effort`; ``role_timeout_s`` via
    :func:`tournament.timeouts.resolve_role_timeout_s`.

    Returns:
        A 4-tuple ``(role_max_turns, role_allowed_tools, role_timeout_s,
        role_effort)``. The ``timeout_s`` dict was added in v0.5.4 to escalate
        the long-reasoning roles on complex plans without inflating the cheap
        ones; it is the third element so the existing ``role_effort``
        position is preserved as last.
    """
    role_max_turns: dict[str, int] = {}
    role_allowed_tools: dict[str, list[str] | None] = {}
    role_timeout_s: dict[str, int] = {}
    role_effort: dict[str, str] = {}

    for role in _TOURNAMENT_ROLES:
        spec = orch.registry.get(role)
        if spec is None:
            continue
        role_max_turns[role] = spec.max_turns or 1
        # v0.41.0 A4: the pure text-only roles (critic_t / synthesizer) are
        # fed their entire working set inline by the content handler, so they
        # must NOT carry Read — a single speculative read at a tiny turn
        # budget exhausts the only turn (error_max_turns → dead branch). Drop
        # Read here at the runner boundary (belt) so the override map is
        # explicit; AdapterLLMClient._resolve_allowed_tools also enforces it
        # (suspenders) by refusing to re-add the ["Read"] sentinel for these
        # roles. architect_b is excluded — it keeps its registry tools.
        # Mirrors :func:`phase_review_runner._build_role_overrides`; without
        # this the plan tournament's critic_t/synthesizer died with
        # ``error_max_turns`` and killed all 3 branches (Run-3/Run-4 A4).
        if role in _TEXT_ONLY_NO_TOOL_ROLES:
            role_allowed_tools[role] = []
        else:
            role_allowed_tools[role] = (
                list(spec.tools) if spec.tools else []
            )
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


def _resolve_tournament_model(orch: "Orchestrator") -> str | None:
    """Return the judge model (or ``None`` if unresolved).

    We use the judge role because (a) it is the dominant cost in a pass
    (N parallel calls) and (b) research shows tournament gains plateau
    above Haiku-tier models.
    """
    spec = orch.registry.get("judge")
    if spec is not None and spec.model:
        return spec.model
    # Fall back to the AgentConfig view in cfg.agents (the registry may have
    # been built from the same source but we look both up for robustness).
    agent_cfg = orch.cfg.agents.get("judge")
    if agent_cfg is not None and agent_cfg.model:
        return agent_cfg.model
    return None


def _is_auto_disabled(model: str | None, auto_disable: list[str]) -> bool:
    """Return ``True`` if ``model`` matches any auto-disable marker.

    Matching is case-insensitive substring so ``"claude-opus-4"`` matches
    ``["opus"]`` — consistent with the observation that tournament gains
    plateau at higher model tiers.
    """
    if not model or not auto_disable:
        return False
    low = model.lower()
    return any(marker.lower() in low for marker in auto_disable)


async def run_plan_tournament(
    orch: "Orchestrator",
    initial_md: str,
    spec: str,
    spec_hash: str,
    *,
    branch_index: int | None = None,
    branch_seed: int | None = None,
    branch_config: "BranchConfig | None" = None,
) -> str:
    """Run the plan tournament and return the refined plan markdown.

    Args:
        orch: Orchestrator instance carrying adapter / config / plugin registry.
        initial_md: Draft plan markdown produced by the architect role.
        spec: Original user intent (used as the tournament ``task_prompt``).
        spec_hash: Stable 16-hex-char digest of the user spec. Used to derive
            a deterministic ``tournament_id`` (so reruns on the same spec land
            in the same artifact dir and can resume) and to seed the
            tournament RNG (so judge presentation order and synthesizer X/Y
            ordering are reproducible across runs).
        branch_index: v0.12.0 — optional branch index for multi-branch
            tournaments. When ``None`` (default), the runner uses the
            legacy single-branch tournament id and ``tournaments/plan-{hash}/``
            artifact dir. When set, uses the branch-namespaced id and
            ``tournaments/multi-{spec_hash[:8]}/branch-N/`` artifact dir
            so concurrent branches don't collide on disk.
        branch_seed: v0.12.0 — optional explicit RNG seed. When ``None``
            (default), seeds from ``int(spec_hash, 16)`` (existing
            behavior). When set, seeds from this value directly so the
            multi-branch dispatcher can pass divergent seeds (e.g.
            ``int(spec_hash, 16) + branch_index``) per branch.
        branch_config: v0.14.0 — optional :class:`config.schema.BranchConfig`
            describing this branch's per-role model overrides plus
            advisory lane / risk / family tags. When set, the per-role
            model resolution consults ``branch_config.model_overrides``
            first; the lane is appended to the artifact dir name as
            ``branch-{index}-{lane}/``. ``None`` (default) preserves
            v0.12.0 homogeneous behavior.

    Behavior:
        - If any relevant role resolves to an auto-disabled model, returns
          ``initial_md`` unchanged and logs ``plan_tournament.auto_disabled``.
        - Otherwise runs the tournament to convergence (or ``max_rounds``)
          and returns the final incumbent.
        - Appends a ``plan_tournament_complete`` ledger entry at the end.
    """
    cfg = orch.cfg.tournaments.plan
    # v0.25.3: consult the per-tournament auto-disable list. The top-level
    # ``tournaments.auto_disable_for_models`` is deprecated; it is now
    # inherited into each per-tournament slot at validation time and the
    # resolved list lives on ``cfg.auto_disable_for_models``. The default
    # for the plan tournament is ``[]`` so the README's #1 discipline
    # mechanism runs even on Opus.
    auto_disable = cfg.auto_disable_for_models or []
    model = _resolve_tournament_model(orch)

    if _is_auto_disabled(model, auto_disable):
        logger.info(
            "plan_tournament.auto_disabled",
            model=model,
            auto_disable_for_models=auto_disable,
        )
        return initial_md

    # v0.26.0: the v0.25.4 InlineAdapter+tournaments defense-in-depth raise
    # was removed alongside InlineAdapter itself — the mismatch can no
    # longer happen.
    tournament_id = _plan_tournament_id(spec_hash, branch_index=branch_index)
    if branch_index is None:
        # Legacy single-branch path: tournaments/plan-{hash}/
        artifact_dir = autodev_root(orch.cwd) / "tournaments" / tournament_id
    else:
        # v0.12.0 multi-branch path: tournaments/multi-{hash}/branch-N/
        # The parent ``multi-{hash}/`` dir keeps branch artifacts grouped
        # without colliding with legacy single-branch dirs.
        # v0.14.0: when a branch_config is supplied, the lane is appended
        # to the dir name (``branch-N-{lane}/``) so the on-disk layout
        # records each branch's divergent trajectory at a glance.
        if branch_config is not None:
            branch_dir_name = f"branch-{branch_index}-{branch_config.lane}"
        else:
            branch_dir_name = f"branch-{branch_index}"
        artifact_dir = (
            autodev_root(orch.cwd)
            / "tournaments"
            / f"multi-{spec_hash[:8]}"
            / branch_dir_name
        )

    # Extract the architect's COMPLEXITY: classification directly from the
    # markdown the tournament is about to refine. The parsed Plan isn't
    # persisted to plan_manager until AFTER this function returns, so
    # plan_manager.load() would still yield None at this point — reading from
    # the markdown is the source-of-truth path during the plan tournament.
    plan_complexity = extract_complexity(initial_md)

    # v0.7.0 / Issue 5C: complexity-aware judge ensemble. When the architect
    # classifies the plan as ``complex`` AND the operator has opted in by
    # setting ``cfg.complex_plan_num_judges_override``, escalate the judge
    # panel for this run. Adopts autoreason's "7 judges → ~3× faster
    # convergence" finding, gated to complex plans so the cost (~40% more
    # judge calls) doesn't apply to medium / simple work.
    effective_num_judges = cfg.num_judges
    if (
        plan_complexity == "complex"
        and cfg.complex_plan_num_judges_override is not None
    ):
        effective_num_judges = cfg.complex_plan_num_judges_override
        logger.info(
            "plan_tournament.judge_ensemble_escalated",
            complexity=plan_complexity,
            default_num_judges=cfg.num_judges,
            override_num_judges=effective_num_judges,
        )

    role_max_turns, role_allowed_tools, role_timeout_s, role_effort = (
        _build_role_overrides(orch, plan_complexity)
    )
    # v0.14.0: per-branch role-model overrides. Empty dict / None
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
    # When the operator hasn't pinned an explicit int, this returns a
    # host-aware value derived from CPU count, available memory, and the
    # judge cohort size — replacing the fixed ``max_parallel_subprocesses=3``
    # cap. The probe is cheap (microseconds) so we can afford a fresh
    # capacity reading at every tournament startup.
    resolved_parallelism = resolve_parallelism(
        configured=orch.cfg.tournaments.max_parallel_subprocesses,
        capacity=probe_host(),
        role_mix="plan",
        num_judges=effective_num_judges,
    )
    # v0.39.0 B3: halve auto-resolved parallelism on huge repos (operator
    # pin bypasses; no-op on small repos / when the escape hatch is set).
    resolved_parallelism = resolve_huge_repo_parallelism(
        base=resolved_parallelism,
        configured=orch.cfg.tournaments.max_parallel_subprocesses,
        cwd=orch.cwd,
        cfg=orch.cfg,
    )
    # F-7: thread the optional plan-phase wall-clock budget into the
    # tournament loop. ``None`` (default) → OFF → byte-identical legacy
    # behavior (no cumulative deadline). When set below an external/benchmark
    # per-command timeout, the loop fails LOUD (emitting
    # ``plan_phase_wall_budget_exceeded``) BEFORE being SIGKILLed.
    plan_phase_wall_budget_s = getattr(
        orch.cfg.guardrails, "plan_phase_wall_budget_s", None
    )
    tcfg = TournamentConfig(
        num_judges=effective_num_judges,
        convergence_k=cfg.convergence_k,
        max_rounds=cfg.max_rounds,
        model=model,
        max_parallel_subprocesses=resolved_parallelism,
        score_stability_window=cfg.score_stability_window,
        score_stability_max_delta=cfg.score_stability_max_delta,
        winner_stability_window=cfg.winner_stability_window,
        max_plan_lines_growth_ratio=cfg.max_plan_lines_growth_ratio,
        wall_budget_s=plan_phase_wall_budget_s,
    )

    judge_plugins = (
        list(orch.plugin_registry.judges.values())
        if orch.plugin_registry is not None
        else []
    )
    # v0.12.0: when ``branch_seed`` is provided (multi-branch dispatch),
    # use it directly so each branch's RNG diverges from its siblings.
    # Otherwise preserve v0.11.x behavior of seeding from the spec hash.
    rng = random.Random(branch_seed if branch_seed is not None else int(spec_hash, 16))
    tournament = Tournament(
        handler=PlanContentHandler(),
        client=client,
        cfg=tcfg,
        artifact_dir=artifact_dir,
        rng=rng,
        judge_plugins=judge_plugins,
    )

    logger.info(
        "plan_tournament.start",
        tournament_id=tournament_id,
        model=model,
        num_judges=tcfg.num_judges,
        convergence_k=tcfg.convergence_k,
        max_rounds=tcfg.max_rounds,
    )

    try:
        final_md, history = await tournament.run(
            task_prompt=spec, initial=initial_md
        )
    except TournamentError as exc:
        # F-7: the cumulative wall-clock ceiling tripped inside the loop. Emit
        # the LOUD, attributable ``plan_phase_wall_budget_exceeded`` ledger op
        # (the greppable reason that replaces an opaque external timeout), then
        # re-raise so the existing plan-phase salvage path recovers the best
        # on-disk incumbent. Other ``TournamentError`` shapes (survivor floor,
        # etc.) propagate unchanged — we only annotate the wall-budget breach.
        if "plan_phase_wall_budget_exceeded" in str(exc):
            await _emit_wall_budget_exceeded(
                orch,
                tournament_id=tournament_id,
                spec_hash=spec_hash,
                branch_index=branch_index,
                budget_s=plan_phase_wall_budget_s,
                reason=str(exc),
            )
        raise

    winner_streak = history[-1].winner if history else None
    logger.info(
        "plan_tournament.done",
        tournament_id=tournament_id,
        passes=len(history),
        winner_last=winner_streak,
        artifact_dir=str(artifact_dir),
    )

    # v0.15.0: emit cross-run lessons from the tournament outcome. Per-pass
    # discards (every non-winning candidate per pass) AND the final
    # winner_promoted event get recorded into the swarm tier so future
    # tournaments + future runs of this project can consult what worked
    # and what was rejected. Errors are swallowed with a warning — a
    # knowledge-write failure must never break a converged tournament.
    try:
        await _emit_plan_tournament_lessons(
            orch,
            tournament_id=tournament_id,
            history=history,
            final_md=final_md,
            initial_md=initial_md,
            branch_index=branch_index,
            branch_config=branch_config,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "plan_tournament.lessons_emit_failed",
            tournament_id=tournament_id,
            error=str(exc),
        )

    # Breadcrumb for resume + observability.
    await orch.plan_manager.ledger_append(
        op="plan_tournament_complete",
        payload={
            "tournament_id": tournament_id,
            "passes": len(history),
            "winner_last": winner_streak,
            "artifact_dir": str(artifact_dir.relative_to(orch.cwd))
            if artifact_dir.is_relative_to(orch.cwd)
            else str(artifact_dir),
        },
    )

    return final_md


async def _emit_wall_budget_exceeded(
    orch: "Orchestrator",
    *,
    tournament_id: str,
    spec_hash: str,
    branch_index: int | None,
    budget_s: float | None,
    reason: str,
) -> None:
    """F-7 fail-loud signal: the plan-tournament wall-clock ceiling tripped.

    Emit a LOUD, attributable ``plan_phase_wall_budget_exceeded`` ledger op —
    the distinct, greppable reason the field analysis needs in place of an
    opaque "timed out after Ns" external SIGKILL. The caller re-raises the
    ``TournamentError`` afterwards so the EXISTING plan-phase salvage path
    recovers the best on-disk incumbent; this breadcrumb does NOT itself
    mutate plan state.

    Best-effort: a breadcrumb failure must never mask the (re-raised) error.
    The full breach ``reason`` (which embeds the elapsed_s / budget_s /
    passes-completed figures) is carried in the payload verbatim; the loop
    already logged the authoritative STRUCTURED values via the
    ``tournament.wall_budget_exceeded`` log line at the breach site.
    """
    logger.warning(
        "plan_tournament.wall_budget_exceeded",
        tournament_id=tournament_id,
        spec_hash=spec_hash,
        branch_index=branch_index,
        budget_s=budget_s,
    )
    try:
        await orch.plan_manager.ledger_append(
            op="plan_phase_wall_budget_exceeded",
            payload={
                "tournament_id": tournament_id,
                "spec_hash": spec_hash,
                "branch_index": branch_index,
                "budget_s": budget_s,
                "reason": reason,
            },
        )
    except Exception as exc:  # noqa: BLE001 - breadcrumb best-effort; never mask
        logger.warning(
            "plan_tournament.ledger_append_failed",
            op="plan_phase_wall_budget_exceeded",
            err=str(exc),
        )


async def _emit_plan_tournament_lessons(
    orch: "Orchestrator",
    *,
    tournament_id: str,
    history: list,
    final_md: str,
    initial_md: str,
    branch_index: int | None,
    branch_config: "BranchConfig | None" = None,
) -> None:
    """Emit cross-run lessons from a completed plan tournament.

    For each pass in ``history``:
        * Records ONE ``discard`` event per non-winning candidate label
          (out of the {A, B, AB} cohort). The lesson body carries the
          Borda scores so future passes can see *how* the discard lost.

    After the loop:
        * Records ONE ``winner_promoted`` event keyed off the trailing
          winner streak label so future runs can prefer it.

    All errors are bubbled to the caller, which logs and swallows them
    — see :func:`run_plan_tournament`'s wrapping ``try/except``.

    v0.18.0 B1: when ``branch_config`` is supplied, every emitted event
    is tagged with ``branch_config.lane`` so :meth:`KnowledgeStore.inject_block`
    can filter by lane during future tournament passes.
    """
    family = "plan-tournament"
    branch_tag = (
        f" branch={branch_index}" if branch_index is not None else ""
    )
    lane = branch_config.lane if branch_config is not None else None

    for pr in history:
        pass_num = getattr(pr, "pass_num", "?")
        winner_label = getattr(pr, "winner", "")
        scores = getattr(pr, "scores", {}) or {}
        candidate_labels = ("A", "B", "AB")
        for label in candidate_labels:
            if label == winner_label:
                continue
            evidence = (
                f"tournament={tournament_id}{branch_tag} pass={pass_num} "
                f"winner={winner_label} loser={label} "
                f"scores={scores}"
            )
            await orch.knowledge.record_tournament_event(
                TournamentEvent(
                    event_type="discard",
                    family=family,
                    hypothesis=(
                        f"candidate {label} did not win pass {pass_num} "
                        f"of tournament {tournament_id}"
                    ),
                    evidence=evidence,
                    rollback_reason=f"borda-loss-to-{winner_label}",
                    lane=lane,
                )
            )

    # Final winner_promoted: indicate the converged plan's identity by
    # length-fingerprint + the trailing winner streak label so the lesson
    # is descriptive enough to be useful but never dumps the full plan
    # markdown into the lessons text (size cap protection).
    final_label = history[-1].winner if history else None
    final_fingerprint = (
        f"len={len(final_md)} "
        f"line_count={len(final_md.splitlines())} "
        f"trailing_winner={final_label}"
    )
    await orch.knowledge.record_tournament_event(
        TournamentEvent(
            event_type="winner_promoted",
            family=family,
            hypothesis=(
                f"tournament {tournament_id}{branch_tag} converged with "
                f"trailing winner {final_label}"
            ),
            evidence=final_fingerprint,
            next_action_hint=(
                "future passes on this spec should prefer the converged "
                "structure over alternatives in the same family"
            ),
            lane=lane,
        )
    )


__all__ = ["_plan_tournament_id", "run_plan_tournament"]
