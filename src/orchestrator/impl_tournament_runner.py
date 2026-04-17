"""Glue between :class:`~orchestrator.Orchestrator` and the Phase-7
tournament engine for implementation-bundle refinement.

Mirrors :mod:`orchestrator.plan_tournament_runner` but wires
:class:`~tournament.ImplTournament` + :class:`WorktreeManager` +
a concrete :class:`_CoderRunner` that re-delegates to the ``developer`` agent
in an isolated git worktree.

Responsibilities:
    - Resolve the effective model for tournament roles and honor
      ``cfg.tournaments.auto_disable_for_models``.
    - Build the :class:`~tournament.llm.AdapterLLMClient` over the
      orchestrator's adapter.
    - Construct :class:`~tournament.ImplTournament` with
      :class:`~tournament.ImplContentHandler`, a :class:`_CoderRunner`,
      and a :class:`WorktreeManager`, then run it.
    - Write :class:`~state.schemas.TournamentEvidence` to
      ``evidence/{task_id}-tournament.json``.
    - Append an ``impl_tournament_complete`` ledger breadcrumb.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from adapters import InlineAdapter
from autologging import get_logger
from orchestrator.delegation_envelope import DelegationEnvelope
from orchestrator.worktree import WorktreeManager
from state.evidence import write_evidence
from state.paths import autodev_root
from state.schemas import TournamentEvidence
from tournament import (
    AdapterLLMClient,
    ImplBundle,
    ImplContentHandler,
    ImplTournament,
    TournamentConfig,
)


if TYPE_CHECKING:
    from orchestrator import Orchestrator
    from state.schemas import Task


logger = get_logger(__name__)


# Tournament roles are called in this order each pass; the judge model is the
# most consequential because it drives the Borda aggregation.
_TOURNAMENT_ROLES: tuple[str, ...] = ("critic_t", "architect_b", "synthesizer", "judge")


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
    """Return ``True`` if ``model`` matches any auto-disable marker.

    Matching is case-insensitive substring so ``"claude-opus-4"`` matches
    ``["opus"]``.
    """
    if not model or not auto_disable:
        return False
    low = model.lower()
    return any(marker.lower() in low for marker in auto_disable)


class _CoderRunner:
    """Concrete :class:`~tournament.CoderRunner` implementation.

    Builds a :class:`DelegationEnvelope` from the task + direction text,
    invokes the adapter with ``cwd=worktree``, runs the test_engineer on
    the produced diff, and returns an :class:`ImplBundle` with the results.
    """

    def __init__(self, orch: "Orchestrator") -> None:
        self._orch = orch
        self._log = get_logger(component="impl_coder_runner")

    async def run(
        self,
        variant_label: str,
        direction: str,
        worktree: Path,
        task: ImplBundle,
    ) -> ImplBundle:
        """Realize a variant by running the developer in the given worktree."""
        from adapters.types import AgentInvocation
        from orchestrator.execute_phase import _parse_test_counts

        orch = self._orch

        # Build developer envelope with direction text injected as context.
        developer_env = DelegationEnvelope(
            task_id=task.task_id,
            target_agent="developer",
            action="implement",
            acceptance=None,
            context={
                "task_description": task.task_description,
                "direction": direction,
                "variant_label": variant_label,
            },
        )

        developer_spec = orch.registry.get("developer")
        if developer_spec is None:
            raise RuntimeError("developer role not in registry")

        developer_prompt = "\n\n---\n".join(
            [
                developer_spec.prompt.strip(),
                developer_env.render_as_task_message(),
            ]
        )

        developer_inv = AgentInvocation(
            role="developer",
            prompt=developer_prompt,
            cwd=worktree,
            model=developer_spec.model,
            allowed_tools=list(developer_spec.tools) if developer_spec.tools else None,
            max_turns=developer_spec.max_turns or 1,
        )
        developer_result = await orch.adapter.execute(developer_inv)

        diff = developer_result.diff or ""
        files_changed = [str(p) for p in (developer_result.files_changed or [])]

        # Run test_engineer on the produced diff.
        test_env = DelegationEnvelope(
            task_id=task.task_id,
            target_agent="test_engineer",
            action="test",
            acceptance=(
                "Run tests and return a line of the form 'RESULTS: passed=N "
                "failed=M total=T'. Include failure output if any test failed."
            ),
            context={
                "task_description": task.task_description,
                "diff": diff[:8000],
                "variant_label": variant_label,
            },
        )

        test_spec = orch.registry.get("test_engineer")
        if test_spec is None:
            raise RuntimeError("test_engineer role not in registry")

        test_prompt = "\n\n---\n".join(
            [
                test_spec.prompt.strip(),
                test_env.render_as_task_message(),
            ]
        )

        test_inv = AgentInvocation(
            role="test_engineer",
            prompt=test_prompt,
            cwd=worktree,
            model=test_spec.model,
            allowed_tools=list(test_spec.tools) if test_spec.tools else None,
            max_turns=test_spec.max_turns or 1,
        )
        test_result = await orch.adapter.execute(test_inv)
        passed, failed, total = _parse_test_counts(test_result.text)

        self._log.info(
            "coder_runner.done",
            variant=variant_label,
            task_id=task.task_id,
            diff_bytes=len(diff),
            passed=passed,
            failed=failed,
        )

        return ImplBundle(
            task_id=task.task_id,
            task_description=task.task_description,
            diff=diff,
            files_changed=files_changed,
            tests_passed=passed,
            tests_failed=failed,
            tests_total=total,
            test_output_excerpt=test_result.text[:1000],
            variant_label=variant_label,  # type: ignore[arg-type]
        )


async def run_impl_tournament(
    orch: "Orchestrator",
    task: "Task",
    initial_bundle: ImplBundle,
) -> ImplBundle:
    """Run the impl tournament and return the refined :class:`ImplBundle`.

    Behavior:
        - If any relevant role resolves to an auto-disabled model, returns
          ``initial_bundle`` unchanged and logs ``impl_tournament.auto_disabled``.
        - Otherwise runs the tournament to convergence (or ``max_rounds``)
          and returns the final incumbent.
        - Writes :class:`TournamentEvidence` to ``evidence/{task_id}-tournament.json``.
        - Appends an ``impl_tournament_complete`` ledger entry at the end.
    """
    cfg = orch.cfg.tournaments.impl
    auto_disable = orch.cfg.tournaments.auto_disable_for_models
    model = _resolve_tournament_model(orch)

    if _is_auto_disabled(model, auto_disable):
        logger.info(
            "impl_tournament.auto_disabled",
            model=model,
            auto_disable_for_models=auto_disable,
        )
        return initial_bundle

    assert not isinstance(orch.adapter, InlineAdapter), (
        "Tournament runners must use subprocess adapters, not InlineAdapter"
    )

    tournament_id = f"impl-{uuid.uuid4().hex[:8]}"
    artifact_dir = autodev_root(orch.cwd) / "tournaments" / tournament_id
    worktree_dir = artifact_dir / "worktrees"

    client = AdapterLLMClient(orch.adapter, cwd=orch.cwd)

    tcfg = TournamentConfig(
        num_judges=cfg.num_judges,
        convergence_k=cfg.convergence_k,
        max_rounds=cfg.max_rounds,
        model=model,
        max_parallel_subprocesses=orch.cfg.tournaments.max_parallel_subprocesses,
    )

    wt_mgr = WorktreeManager(main_repo=orch.cwd, tournament_dir=worktree_dir)
    coder_runner = _CoderRunner(orch)

    tournament = ImplTournament(
        handler=ImplContentHandler(),
        client=client,
        cfg=tcfg,
        artifact_dir=artifact_dir,
        coder_runner=coder_runner,
        worktree_manager=wt_mgr,
    )

    logger.info(
        "impl_tournament.start",
        tournament_id=tournament_id,
        task_id=task.id,
        model=model,
        num_judges=tcfg.num_judges,
        convergence_k=tcfg.convergence_k,
        max_rounds=tcfg.max_rounds,
    )

    try:
        final_bundle, history = await tournament.run(
            task_prompt=task.description,
            initial=initial_bundle,
        )
    finally:
        await wt_mgr.cleanup_all()

    winner_streak = history[-1].winner if history else "A"
    converged = len(history) < tcfg.max_rounds or (
        history[-1].winner == "A"
        and sum(1 for h in reversed(history) if h.winner == "A") >= tcfg.convergence_k
    )

    logger.info(
        "impl_tournament.done",
        tournament_id=tournament_id,
        task_id=task.id,
        passes=len(history),
        winner_last=winner_streak,
        artifact_dir=str(artifact_dir),
    )

    # Write TournamentEvidence.
    t_ev = TournamentEvidence(
        task_id=task.id,
        tournament_id=tournament_id,
        phase="impl",
        passes=len(history),
        winner=winner_streak,  # type: ignore[arg-type]
        converged=converged,
        history=[h.model_dump(mode="json") for h in history],
        final_diff=final_bundle.diff or None,
    )
    await write_evidence(orch.cwd, task.id, t_ev)

    # Breadcrumb for resume + observability.
    await orch.plan_manager.ledger_append(
        op="impl_tournament_complete",
        payload={
            "tournament_id": tournament_id,
            "task_id": task.id,
            "passes": len(history),
            "winner_last": winner_streak,
            "artifact_dir": str(artifact_dir.relative_to(orch.cwd))
            if artifact_dir.is_relative_to(orch.cwd)
            else str(artifact_dir),
        },
    )

    return final_bundle


__all__ = [
    "_is_auto_disabled",
    "_resolve_tournament_model",
    "run_impl_tournament",
]
