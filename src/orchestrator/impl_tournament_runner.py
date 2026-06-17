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

import asyncio
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from autologging import get_logger
from errors import TournamentError
from orchestrator.delegation_envelope import DelegationEnvelope
from orchestrator.huge_repo_overrides import resolve_huge_repo_parallelism
from orchestrator.worktree import WorktreeManager
from runtime.resource_probe import probe_host, resolve_parallelism
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
from tournament.effort import resolve_role_effort
from tournament.prompts import build_developer_prompt
from tournament.timeouts import resolve_role_timeout_s


if TYPE_CHECKING:
    from config.schema import BranchConfig
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


def _resolve_wm_huge_mode(orch: "Orchestrator") -> bool:
    """Resolve the WorktreeManager ``huge_mode`` flag for *orch*.

    v0.40.0 (huge-repo Gap 3): mirrors the resolution the execute-phase
    dispatcher does inline (``worktree_huge_repo_mode`` ``on``/``off``/
    ``auto``, where ``auto`` keys off ``RepoCapacity.is_huge``). Centralized
    here so the impl-tournament worktree path makes the SAME huge-mode
    decision as the execute path rather than defaulting to ``False`` (the
    bug: the tournament ``WorktreeManager`` was built with no ``huge_mode``,
    so ``git worktree add`` kept the 60 s timeout and was killed on the
    Unity LFS repo).
    """
    mode = getattr(orch.cfg, "worktree_huge_repo_mode", "auto")
    if mode == "on":
        return True
    if mode == "off":
        return False
    # "auto"
    return bool(getattr(getattr(orch, "_repo_capacity", None), "is_huge", False))


def _task_sparse_cone(task: "Task") -> list[str] | None:
    """Repo-relative sparse cone from the task's own claimed files.

    v0.40.0 (huge-repo Gap 3): the same task-cone fallback the execute path
    uses (``execute_phase.sparse_cone_from_task_files``) — union the task's
    ``files`` + ``extended_scope`` (deduped, order-preserving). Returned as
    ``None`` when the task declares no files, so the tournament worktree
    falls back to a full checkout exactly as before on a task with no
    declared scope (and on small repos the manager ignores it anyway).
    """
    raw = list(getattr(task, "files", []) or []) + list(
        getattr(task, "extended_scope", []) or []
    )
    seen: set[str] = set()
    cone = [
        p
        for p in raw
        if isinstance(p, str) and p.strip() and not (p in seen or seen.add(p))  # type: ignore[func-returns-value]  # set.add returns None (falsy) by design — dedup idiom
    ]
    return cone or None


def _is_auto_disabled(model: str | None, auto_disable: list[str]) -> bool:
    """Return ``True`` if ``model`` matches any auto-disable marker.

    Matching is case-insensitive substring so ``"claude-opus-4"`` matches
    ``["opus"]``.
    """
    if not model or not auto_disable:
        return False
    low = model.lower()
    return any(marker.lower() in low for marker in auto_disable)


async def _build_tournament_role_overrides(
    orch: "Orchestrator",
) -> tuple[
    dict[str, int],
    dict[str, list[str] | None],
    dict[str, int],
    dict[str, str],
]:
    """Build per-role overrides for the tournament's :class:`AdapterLLMClient`.

    Mirrors :func:`plan_tournament_runner._build_role_overrides`. Roles that
    aren't in the registry are omitted; the client then uses its defaults.

    Returns a 4-tuple
    ``(role_max_turns, role_allowed_tools, role_timeout_s, role_effort)``.
    The third dict (``role_timeout_s``) was added in v0.5.4 and is populated
    from :func:`tournament.timeouts.resolve_role_timeout_s` keyed on the
    parsed plan complexity. The fourth (``role_effort``) is populated from
    :func:`tournament.effort.resolve_role_effort`.
    """
    role_max_turns: dict[str, int] = {}
    role_allowed_tools: dict[str, list[str] | None] = {}
    role_timeout_s: dict[str, int] = {}
    role_effort: dict[str, str] = {}

    plan_complexity: str | None = None
    if orch.plan_manager is not None:
        try:
            existing_plan = await orch.plan_manager.load()
        except Exception:  # noqa: BLE001
            existing_plan = None
        if existing_plan is not None:
            plan_complexity = existing_plan.complexity

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
                build_developer_prompt(developer_spec.prompt),
                developer_env.render_as_task_message(),
            ]
        )

        # Resolve per-role effort once for both developer and test_engineer
        # below. The plan exists by the time the impl tournament runs.
        plan_complexity: str | None = None
        if orch.plan_manager is not None:
            try:
                existing_plan = await orch.plan_manager.load()
            except Exception:  # noqa: BLE001
                existing_plan = None
            if existing_plan is not None:
                plan_complexity = existing_plan.complexity

        developer_effort = resolve_role_effort(
            "developer",
            orch.cfg.agents.get("developer"),
            plan_complexity,
            orch.cfg.user_complexity,
        )

        developer_inv = AgentInvocation(
            role="developer",
            prompt=developer_prompt,
            cwd=worktree,
            model=developer_spec.model,
            allowed_tools=list(developer_spec.tools) if developer_spec.tools else None,
            max_turns=developer_spec.max_turns or 1,
            effort=developer_effort,
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

        test_effort = resolve_role_effort(
            "test_engineer",
            orch.cfg.agents.get("test_engineer"),
            plan_complexity,
            orch.cfg.user_complexity,
        )

        test_inv = AgentInvocation(
            role="test_engineer",
            prompt=test_prompt,
            cwd=worktree,
            model=test_spec.model,
            allowed_tools=list(test_spec.tools) if test_spec.tools else None,
            max_turns=test_spec.max_turns or 1,
            effort=test_effort,
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
    *,
    branch_config: "BranchConfig | None" = None,
) -> ImplBundle:
    """Run the impl tournament and return the refined :class:`ImplBundle`.

    Behavior:
        - If any relevant role resolves to an auto-disabled model, returns
          ``initial_bundle`` unchanged and logs ``impl_tournament.auto_disabled``.
        - Otherwise runs the tournament to convergence (or ``max_rounds``)
          and returns the final incumbent.
        - Writes :class:`TournamentEvidence` to ``evidence/{task_id}-tournament.json``.
        - Appends an ``impl_tournament_complete`` ledger entry at the end.

    Args:
        orch: Orchestrator carrying adapter / config / plugin registry.
        task: Task being implemented.
        initial_bundle: Variant A (incumbent) bundle.
        branch_config: v0.18.0 A1 — optional per-branch config providing
            heterogeneous-model overrides + lane tag. ``None`` (default)
            preserves v0.17.0 homogeneous behavior. When set, the runner
            threads ``branch_config.model_overrides`` into the
            :class:`AdapterLLMClient` and suffixes the artifact dir with
            ``-{lane}`` so on-disk forensics record the divergent
            trajectory.
    """
    cfg = orch.cfg.tournaments.impl
    # v0.25.3: per-tournament auto-disable list. impl defaults to
    # ``["opus"]`` because impl tournaments fire once per task and can
    # blow through budget on Opus.
    auto_disable = cfg.auto_disable_for_models or []
    model = _resolve_tournament_model(orch)

    if _is_auto_disabled(model, auto_disable):
        logger.info(
            "impl_tournament.auto_disabled",
            model=model,
            auto_disable_for_models=auto_disable,
        )
        return initial_bundle

    # v0.26.0: the v0.25.4 InlineAdapter+tournaments defense-in-depth raise
    # was removed alongside InlineAdapter itself.
    tournament_id = f"impl-{uuid.uuid4().hex[:8]}"
    # v0.18.0 A1: when a branch_config is supplied, suffix the artifact dir
    # with the lane label so the on-disk layout records each branch's
    # divergent trajectory at a glance.
    if branch_config is not None:
        artifact_dir_name = f"{tournament_id}-{branch_config.lane}"
    else:
        artifact_dir_name = tournament_id
    artifact_dir = autodev_root(orch.cwd) / "tournaments" / artifact_dir_name
    worktree_dir = artifact_dir / "worktrees"

    role_max_turns, role_allowed_tools, role_timeout_s, role_effort = (
        await _build_tournament_role_overrides(orch)
    )

    # v0.18.0 A1: per-branch role-model overrides. Empty dict / None
    # preserves legacy homogeneous behavior — the global ``model`` passed
    # into ``client.call`` is used for every role.
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

    # v0.7.0 / Issue 5C: ``complex_plan_num_judges_override`` is a plan-only
    # knob — impl complexity isn't extracted from a plan markdown — but the
    # field is read here for symmetry so the impl runner stays parallel to
    # the plan runner. ``num_judges`` is always ``cfg.num_judges`` for impl;
    # we don't escalate based on the parsed Plan's complexity here because
    # the impl tournament's role mix is structurally different (single-judge
    # by convention, with worktree variants doing the heavy lifting).
    _impl_complex_override_unused = cfg.complex_plan_num_judges_override
    del _impl_complex_override_unused

    # v0.10.0: resolve subprocess parallelism via the runtime probe.
    # See :mod:`orchestrator.plan_tournament_runner` for the rationale.
    # Note: impl tournaments are single-judge by convention so the
    # cohort cap is always 1; the resolver still runs to honor explicit
    # operator pins and to log ``tournament.parallelism_resolved`` for
    # forensics consistency across the three runner surfaces.
    resolved_parallelism = resolve_parallelism(
        configured=orch.cfg.tournaments.max_parallel_subprocesses,
        capacity=probe_host(),
        role_mix="impl",
        num_judges=cfg.num_judges,
    )
    # v0.39.0 B3: halve auto-resolved parallelism on huge repos (operator
    # pin bypasses; no-op on small repos / when the escape hatch is set).
    resolved_parallelism = resolve_huge_repo_parallelism(
        base=resolved_parallelism,
        configured=orch.cfg.tournaments.max_parallel_subprocesses,
        cwd=orch.cwd,
        cfg=orch.cfg,
    )
    # v0.18.0 C3: when veto-mode is active, use specialist judge roles by
    # default. Operators can override via ``cfg.tournaments.impl.judge_roles``
    # to set their own cohort. Otherwise fall through to the legacy
    # ``["judge"] * num_judges`` cohort.
    judge_roles_resolved: list[str] | None = None
    if getattr(cfg, "voting_strategy", "borda") == "veto":
        judge_roles_resolved = (
            list(cfg.judge_roles) if cfg.judge_roles else
            ["critic", "reviewer", "test_engineer", "domain_expert", "explorer"]
        )
    elif cfg.judge_roles:
        judge_roles_resolved = list(cfg.judge_roles)

    effective_num_judges = (
        len(judge_roles_resolved) if judge_roles_resolved else cfg.num_judges
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
        # Impl tournaments default ``max_plan_lines_growth_ratio=None``
        # (impl artifacts are diff bundles, not line-counted plan markdown);
        # plumbed through for symmetry with ``TournamentConfig``.
        max_plan_lines_growth_ratio=cfg.max_plan_lines_growth_ratio,
        judge_roles=judge_roles_resolved,
        judge_role_weights=(
            dict(cfg.judge_role_weights) if cfg.judge_role_weights else None
        ),
        # v0.22.0 Phase 4 (anti-bloat): forward the absolute-token-cap
        # demotion threshold from the schema-level config to the runtime
        # TournamentConfig. ``getattr`` with default keeps this safe if a
        # legacy on-disk config doesn't carry the new field.
        oversized_demotion_token_threshold=getattr(
            cfg, "oversized_demotion_token_threshold", 0
        ),
    )

    # v0.40.0 (huge-repo Gap 3): build the tournament WorktreeManager
    # huge-safe — same as the execute-phase path. Previously this manager
    # was constructed with no ``huge_mode`` (→ 60 s ``git worktree add``
    # timeout) and the engine called ``create(nonce, base_ref="HEAD")`` with
    # no scope (→ full checkout). On the Unity LFS repo that timed out at
    # 60 s, the killed git op left a stale ``.git/index.lock``, and every
    # subsequent ``git apply`` failed. Now: extended timeout on huge repos
    # + a default sparse cone from the task's files so the engine's
    # scope-less ``create`` narrows the checkout via the same machinery the
    # ``create_per_task`` path uses.
    _wm_huge_mode = _resolve_wm_huge_mode(orch)
    _wm_huge_timeout_s = float(
        getattr(orch.cfg, "worktree_huge_create_timeout_s", 600)
    )
    # Sparse becomes the default on huge repos (mirrors the execute path's
    # ``worktree_huge_repo_mode``-driven flip); the legacy
    # ``worktree_sparse_checkout_enabled`` flag is an explicit opt-in for
    # non-huge repos. Only compute the cone when sparse is in effect so
    # small-repo tournaments stay full-checkout (no behavior change).
    _sparse_enabled = bool(
        getattr(orch.cfg, "worktree_sparse_checkout_enabled", False)
    ) or _wm_huge_mode
    _default_cone = _task_sparse_cone(task) if _sparse_enabled else None
    if _default_cone:
        logger.info(
            "impl_tournament.sparse_cone_from_task_files",
            task_id=task.id,
            paths=_default_cone,
        )
    wt_mgr = WorktreeManager(
        main_repo=orch.cwd,
        tournament_dir=worktree_dir,
        huge_mode=_wm_huge_mode,
        huge_create_timeout_s=_wm_huge_timeout_s,
        autodev_root=autodev_root(orch.cwd),
        default_sparse_paths=_default_cone,
    )
    coder_runner = _CoderRunner(orch)

    judge_plugins = (
        list(orch.plugin_registry.judges.values())
        if orch.plugin_registry is not None
        else []
    )

    # v0.18.0 C1: opt into the council/veto strategy when the operator
    # configures ``cfg.tournaments.impl.voting_strategy = "veto"``. Default
    # ``"borda"`` preserves v0.17.0 byte-identical aggregation behavior.
    voting_strategy: Any = None
    if getattr(cfg, "voting_strategy", "borda") == "veto":
        from tournament.voting import VetoAggregator

        # v0.18.0 C2: when veto is active and the architect populated
        # Task.acceptance, persist criteria to a council sidecar JSON for
        # forensics + per-criterion vote tracking.
        if task.acceptance:
            try:
                import json as _json

                from state.paths import council_criteria_path

                sidecar = council_criteria_path(orch.cwd, task.id)
                sidecar.parent.mkdir(parents=True, exist_ok=True)
                sidecar.write_text(
                    _json.dumps(
                        {
                            "task_id": task.id,
                            "criteria": [
                                ac.model_dump(mode="json")
                                for ac in task.acceptance
                            ],
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                voting_strategy = VetoAggregator(criteria=list(task.acceptance))
            except Exception as exc:  # noqa: BLE001 — sidecar is forensics-only
                logger.warning(
                    "impl_tournament.council_sidecar_write_failed",
                    task_id=task.id,
                    err=str(exc),
                )
                voting_strategy = VetoAggregator()
        else:
            # No acceptance criteria → veto agg behaves as plain
            # last-place-rejects-candidate policy without per-criterion
            # bookkeeping.
            logger.warning(
                "impl_tournament.veto_without_criteria",
                task_id=task.id,
            )
            voting_strategy = VetoAggregator()

    tournament = ImplTournament(
        handler=ImplContentHandler(),
        client=client,
        cfg=tcfg,
        artifact_dir=artifact_dir,
        coder_runner=coder_runner,
        worktree_manager=wt_mgr,
        judge_plugins=judge_plugins,
        voting_strategy=voting_strategy,
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


async def run_multi_branch_impl_tournament(
    orch: "Orchestrator",
    task: "Task",
    initial_bundle: ImplBundle,
    n_branches: int,
    branch_configs: "list[BranchConfig] | None" = None,
) -> ImplBundle:
    """v0.21.0 A2: run N parallel impl tournaments, then meta-merge survivors.

    Mirrors :func:`run_multi_branch_plan_tournament` for impl scope. The
    high-level flow:

    1. Append ``multi_branch_impl_start`` ledger op.
    2. ``asyncio.gather`` N copies of :func:`run_impl_tournament`,
       each in its own per-branch worktree (when applicable) with
       ``return_exceptions=True`` so a single branch failure does NOT
       cancel siblings.
    3. Apply the survivor floor ``max(2, ceil(N/2))`` — fewer surviving
       branches → :class:`TournamentError` raised so the caller falls
       back to the v0.6.0 salvage path.
    4. **Meta-merge via diff synthesis**: not git 3-way merge (conflict
       risk on real code), not Borda-only on diffs (loses information).
       Instead, the synthesizer LLM call sees N candidate diffs side-by-
       side, produces merged-diff markdown, then a fresh ``CoderRunner``
       re-materializes the merged diff in a clean worktree to produce
       the final :class:`ImplBundle`.
    5. Append ``multi_branch_impl_meta_merge_complete`` and
       ``multi_branch_impl_complete`` ledger ops.

    Branch-config alignment is the same contract as
    :func:`run_multi_branch_plan_tournament`: when ``branch_configs`` is
    non-None, ``len(branch_configs) == n_branches`` is required.

    Survivor-floor failure raises :class:`TournamentError`. The caller
    (the execute-phase impl-tournament dispatch site) is expected to
    catch and either fall back to the original ``initial_bundle`` or
    activate the salvage path that scans
    ``tournaments/multi-impl-{task_id_prefix}/branch-N/`` for the best
    surviving incumbent.

    Args:
        orch: Orchestrator carrying adapter, cfg, registry.
        task: Task being implemented.
        initial_bundle: Variant A (incumbent) bundle.
        n_branches: Number of parallel branches.
        branch_configs: Optional list of per-branch model overrides; one
            entry per branch when supplied.
    """
    if n_branches < 1:
        raise ValueError(f"n_branches must be ≥1, got {n_branches}")

    if branch_configs is not None and len(branch_configs) != n_branches:
        raise ValueError(
            f"len(branch_configs) ({len(branch_configs)}) must equal "
            f"n_branches ({n_branches}) — exact 1:1 correspondence required"
        )

    # N=1 short-circuit: pass-through to single-branch runner so the
    # multi-branch path is purely additive.
    if n_branches == 1:
        only_bc = branch_configs[0] if branch_configs is not None else None
        return await run_impl_tournament(
            orch, task, initial_bundle, branch_config=only_bc
        )

    lanes: list[str | None]
    if branch_configs is not None:
        lanes = [bc.lane if bc is not None else None for bc in branch_configs]
    else:
        lanes = [None] * n_branches

    await orch.plan_manager.ledger_append(
        op="multi_branch_impl_start",
        payload={
            "task_id": task.id,
            "n_branches": n_branches,
            "lanes": lanes,
        },
    )

    logger.info(
        "multi_branch_impl.start",
        task_id=task.id,
        n_branches=n_branches,
    )

    # Step 1: gather N parallel branches with return_exceptions so a
    # single branch failure does NOT cancel siblings.
    coros = [
        run_impl_tournament(
            orch,
            task,
            initial_bundle,
            branch_config=(
                branch_configs[i] if branch_configs is not None else None
            ),
        )
        for i in range(n_branches)
    ]
    raw_results = await asyncio.gather(*coros, return_exceptions=True)

    branches: list[tuple[int, ImplBundle | None, str | None]] = []
    for i, r in enumerate(raw_results):
        if isinstance(r, BaseException):
            branches.append((i, None, str(r)))
            logger.warning(
                "multi_branch_impl.branch_failed",
                branch_index=i,
                err=str(r),
            )
        else:
            branches.append((i, r, None))

    survivors = [(i, b) for (i, b, e) in branches if b is not None]
    floor = _impl_survivor_floor(n_branches)
    if len(survivors) < floor:
        logger.warning(
            "multi_branch_impl.under_floor",
            survivors=len(survivors),
            floor=floor,
            n_branches=n_branches,
        )
        raise TournamentError(
            f"only {len(survivors)} of {n_branches} impl branches succeeded; "
            f"survivor floor is {floor}"
        )

    # Step 2: meta-merge via diff synthesis.
    survivor_diffs = [b.diff or "" for (_, b) in survivors]
    merged_bundle = await _impl_meta_merge_via_diff_synthesis(
        orch,
        task,
        initial_bundle,
        survivor_diffs,
    )

    await orch.plan_manager.ledger_append(
        op="multi_branch_impl_meta_merge_complete",
        payload={
            "task_id": task.id,
            "n_survivors": len(survivors),
            "n_branches": n_branches,
        },
    )
    await orch.plan_manager.ledger_append(
        op="multi_branch_impl_complete",
        payload={
            "task_id": task.id,
            "n_branches": n_branches,
            "n_survivors": len(survivors),
            "winner_diff_bytes": len(merged_bundle.diff or ""),
        },
    )

    logger.info(
        "multi_branch_impl.done",
        task_id=task.id,
        n_branches=n_branches,
        n_survivors=len(survivors),
        winner_diff_bytes=len(merged_bundle.diff or ""),
    )

    return merged_bundle


def _impl_survivor_floor(n_branches: int) -> int:
    """``max(2, ceil(N/2))`` — same shape as plan-side multi-branch."""
    import math

    return max(2, math.ceil(n_branches / 2))


async def _impl_meta_merge_via_diff_synthesis(
    orch: "Orchestrator",
    task: "Task",
    initial_bundle: ImplBundle,
    diffs: list[str],
) -> ImplBundle:
    """v0.21.0 A2: synthesizer-LLM-on-diffs meta-merge.

    Renders the diff-synthesis prompt over the survivor diffs, calls the
    synthesizer role, parses the merged-diff markdown, then re-runs the
    coder in a fresh worktree to materialize a new :class:`ImplBundle`.

    Fallback chain:
        1. ``len(diffs) == 1`` → return a clone of the initial_bundle
           with that diff (no synthesis needed).
        2. Synthesizer raises / returns unparseable diff → fall back to
           the strongest survivor by tests_passed (Borda surrogate over
           pre-merge metrics).
        3. CoderRunner materialization fails → return the strongest
           survivor.
    """
    if not diffs:
        # Defensive: caller should have raised survivor floor before
        # reaching this. Return initial_bundle untouched.
        return initial_bundle

    # ── Build LLM client for the synthesizer call ──
    role_max_turns, role_allowed_tools, role_timeout_s, role_effort = (
        await _build_tournament_role_overrides(orch)
    )
    client = AdapterLLMClient(
        orch.adapter,
        cwd=orch.cwd,
        role_max_turns=role_max_turns,
        role_allowed_tools=role_allowed_tools,
        role_effort=role_effort,
        role_timeout_s=role_timeout_s,
    )

    handler = ImplContentHandler()
    user_prompt = handler.render_for_diff_synthesis(
        task_prompt=task.description,
        diffs=diffs,
    )

    from tournament.prompts import SYNTHESIZER_SYSTEM

    synth_model = _resolve_tournament_model(orch)
    try:
        synth_text = await client.call(
            system=SYNTHESIZER_SYSTEM,
            user=user_prompt,
            role="synthesizer",
            model=synth_model,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "multi_branch_impl.meta_merge.synth_failed",
            task_id=task.id,
            err=str(exc),
        )
        return _fallback_strongest_survivor(initial_bundle, diffs)

    merged_diff = _extract_diff_block(synth_text)
    if not merged_diff:
        logger.warning(
            "multi_branch_impl.meta_merge.no_diff_block",
            task_id=task.id,
            synth_text_excerpt=synth_text[:200] if synth_text else "",
        )
        return _fallback_strongest_survivor(initial_bundle, diffs)

    # ── Re-materialize via CoderRunner in a fresh worktree ──
    artifact_dir = (
        autodev_root(orch.cwd)
        / "tournaments"
        / f"multi-impl-{task.id}-meta"
    )
    worktree_dir = artifact_dir / "worktrees"
    # v0.40.0 (huge-repo Gap 3): the meta-merge worktree is the same
    # scope-less ``create("meta")`` shape, so make it huge-safe too.
    _mm_huge_mode = _resolve_wm_huge_mode(orch)
    _mm_huge_timeout_s = float(
        getattr(orch.cfg, "worktree_huge_create_timeout_s", 600)
    )
    _mm_sparse_enabled = bool(
        getattr(orch.cfg, "worktree_sparse_checkout_enabled", False)
    ) or _mm_huge_mode
    _mm_default_cone = _task_sparse_cone(task) if _mm_sparse_enabled else None
    wt_mgr = WorktreeManager(
        main_repo=orch.cwd,
        tournament_dir=worktree_dir,
        huge_mode=_mm_huge_mode,
        huge_create_timeout_s=_mm_huge_timeout_s,
        autodev_root=autodev_root(orch.cwd),
        default_sparse_paths=_mm_default_cone,
    )
    coder_runner = _CoderRunner(orch)

    try:
        wt = await wt_mgr.create("meta")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "multi_branch_impl.meta_merge.worktree_failed",
            task_id=task.id,
            err=str(exc),
        )
        return _fallback_strongest_survivor(initial_bundle, diffs)

    try:
        # Pass merged diff as the developer's "direction" — the coder
        # role re-implements per the merged diff and runs tests.
        merged = await coder_runner.run(
            variant_label="AB",
            direction=(
                "META-MERGE DIRECTIVE — apply the following merged diff "
                f"into the worktree. Run tests after.\n\n```diff\n"
                f"{merged_diff}\n```"
            ),
            worktree=wt,
            task=initial_bundle,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "multi_branch_impl.meta_merge.coder_failed",
            task_id=task.id,
            err=str(exc),
        )
        return _fallback_strongest_survivor(initial_bundle, diffs)
    finally:
        try:
            await wt_mgr.cleanup_all()
        except Exception:  # noqa: BLE001
            pass

    return merged


def _fallback_strongest_survivor(
    initial_bundle: ImplBundle,
    diffs: list[str],
) -> ImplBundle:
    """Return a clone of ``initial_bundle`` carrying the largest survivor diff.

    Used when meta-merge fails. Picks the longest diff as a stand-in for
    "richest survivor" — a crude but conservative surrogate when the
    survivors carry no ranking metadata at this layer.
    """
    if not diffs:
        return initial_bundle
    best_diff = max(diffs, key=len)
    return ImplBundle(
        task_id=initial_bundle.task_id,
        task_description=initial_bundle.task_description,
        diff=best_diff,
        files_changed=list(initial_bundle.files_changed),
        tests_passed=initial_bundle.tests_passed,
        tests_failed=initial_bundle.tests_failed,
        tests_total=initial_bundle.tests_total,
        test_output_excerpt=initial_bundle.test_output_excerpt,
        variant_label="AB",
        notes="meta-merge-fallback",
    )


def _extract_diff_block(text: str) -> str:
    """Extract the first fenced ``diff`` block from ``text``.

    Looks for ```diff ... ``` (case-insensitive opener). When no fenced
    block is present, falls back to looking for a ``diff --git ...``
    prefix and returning everything from there to end-of-text. Returns
    empty string when no diff-shaped content is found — caller falls
    back to the strongest-survivor path.
    """
    if not text:
        return ""
    # 1. Fenced ```diff ... ``` (preferred shape).
    import re

    m = re.search(
        r"```\s*diff\s*\n(.*?)```",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if m is not None:
        return m.group(1).strip()
    # 2. Generic fenced block (```\n...\n```), use only if it contains
    #    a ``diff --git`` marker so we don't grab prose.
    m2 = re.search(r"```\s*\n(.*?)```", text, re.DOTALL)
    if m2 is not None and "diff --git" in m2.group(1):
        return m2.group(1).strip()
    # 3. Bare ``diff --git`` prefix.
    idx = text.find("diff --git")
    if idx >= 0:
        return text[idx:].strip()
    return ""


__all__ = [
    "_extract_diff_block",
    "_impl_survivor_floor",
    "_is_auto_disabled",
    "_resolve_tournament_model",
    "run_impl_tournament",
    "run_multi_branch_impl_tournament",
]
