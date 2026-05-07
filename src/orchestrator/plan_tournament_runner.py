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

from adapters import InlineAdapter
from autologging import get_logger
from orchestrator.plan_parser import extract_complexity
from state.paths import autodev_root
from tournament import (
    AdapterLLMClient,
    PlanContentHandler,
    Tournament,
    TournamentConfig,
)
from tournament.effort import resolve_role_effort
from tournament.timeouts import resolve_role_timeout_s


if TYPE_CHECKING:
    from orchestrator import Orchestrator


logger = get_logger(__name__)


# Tournament roles are called in this order each pass; the judge model is the
# most consequential because it drives the Borda aggregation. We resolve the
# judge role as the "representative" model for the auto-disable check.
_TOURNAMENT_ROLES: tuple[str, ...] = ("critic_t", "architect_b", "synthesizer", "judge")


def _plan_tournament_id(spec_hash: str) -> str:
    """Derive the deterministic plan-tournament id from a spec hash.

    Centralised so :mod:`orchestrator.plan_phase` (which reads the salvage
    artifacts on a tournament failure) and the runner itself can never drift
    in their notion of "where this tournament lives on disk."
    """
    return f"plan-{spec_hash[:8]}"


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
    orch: "Orchestrator", initial_md: str, spec: str, spec_hash: str
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

    Behavior:
        - If any relevant role resolves to an auto-disabled model, returns
          ``initial_md`` unchanged and logs ``plan_tournament.auto_disabled``.
        - Otherwise runs the tournament to convergence (or ``max_rounds``)
          and returns the final incumbent.
        - Appends a ``plan_tournament_complete`` ledger entry at the end.
    """
    cfg = orch.cfg.tournaments.plan
    auto_disable = orch.cfg.tournaments.auto_disable_for_models
    model = _resolve_tournament_model(orch)

    if _is_auto_disabled(model, auto_disable):
        logger.info(
            "plan_tournament.auto_disabled",
            model=model,
            auto_disable_for_models=auto_disable,
        )
        return initial_md

    assert not isinstance(orch.adapter, InlineAdapter), (
        "Tournament runners must use subprocess adapters, not InlineAdapter"
    )

    tournament_id = _plan_tournament_id(spec_hash)
    artifact_dir = autodev_root(orch.cwd) / "tournaments" / tournament_id

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
    client = AdapterLLMClient(
        orch.adapter,
        cwd=orch.cwd,
        role_max_turns=role_max_turns,
        role_allowed_tools=role_allowed_tools,
        role_effort=role_effort,
        role_timeout_s=role_timeout_s,
    )

    tcfg = TournamentConfig(
        num_judges=effective_num_judges,
        convergence_k=cfg.convergence_k,
        max_rounds=cfg.max_rounds,
        model=model,
        max_parallel_subprocesses=orch.cfg.tournaments.max_parallel_subprocesses,
        score_stability_window=cfg.score_stability_window,
        score_stability_max_delta=cfg.score_stability_max_delta,
        winner_stability_window=cfg.winner_stability_window,
        max_plan_lines_growth_ratio=cfg.max_plan_lines_growth_ratio,
    )

    judge_plugins = (
        list(orch.plugin_registry.judges.values())
        if orch.plugin_registry is not None
        else []
    )
    rng = random.Random(int(spec_hash, 16))
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

    final_md, history = await tournament.run(task_prompt=spec, initial=initial_md)

    winner_streak = history[-1].winner if history else None
    logger.info(
        "plan_tournament.done",
        tournament_id=tournament_id,
        passes=len(history),
        winner_last=winner_streak,
        artifact_dir=str(artifact_dir),
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


__all__ = ["_plan_tournament_id", "run_plan_tournament"]
