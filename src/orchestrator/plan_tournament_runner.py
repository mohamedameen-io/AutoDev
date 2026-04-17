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

import uuid
from typing import TYPE_CHECKING

from adapters import InlineAdapter
from autologging import get_logger
from state.paths import autodev_root
from tournament import (
    AdapterLLMClient,
    PlanContentHandler,
    Tournament,
    TournamentConfig,
)


if TYPE_CHECKING:
    from orchestrator import Orchestrator


logger = get_logger(__name__)


# Tournament roles are called in this order each pass; the judge model is the
# most consequential because it drives the Borda aggregation. We resolve the
# judge role as the "representative" model for the auto-disable check.
_TOURNAMENT_ROLES: tuple[str, ...] = ("critic_t", "architect_b", "synthesizer", "judge")


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


async def run_plan_tournament(orch: "Orchestrator", initial_md: str, spec: str) -> str:
    """Run the plan tournament and return the refined plan markdown.

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

    tournament_id = f"plan-{uuid.uuid4().hex[:8]}"
    artifact_dir = autodev_root(orch.cwd) / "tournaments" / tournament_id

    client = AdapterLLMClient(orch.adapter, cwd=orch.cwd)

    tcfg = TournamentConfig(
        num_judges=cfg.num_judges,
        convergence_k=cfg.convergence_k,
        max_rounds=cfg.max_rounds,
        model=model,
        max_parallel_subprocesses=orch.cfg.tournaments.max_parallel_subprocesses,
    )

    tournament = Tournament(
        handler=PlanContentHandler(),
        client=client,
        cfg=tcfg,
        artifact_dir=artifact_dir,
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


__all__ = ["run_plan_tournament"]
