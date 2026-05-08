"""v0.12.0 multi-branch plan-tournament orchestrator.

Runs N independent RNG-seeded plan tournaments concurrently, then
meta-merges their final outputs via a pairwise reduction over the existing
:class:`~tournament.plan_tournament.PlanContentHandler` synthesizer.

Adopts the autoreason "batch" pattern: each branch explores a divergent
trajectory (seed = ``int(spec_hash, 16) + branch_index``) and the
meta-merge picks the strongest synthesis. With the user-locked-in default
``num_branches=3``, this triples LLM call volume per plan-phase but
typically yields one-or-more pass-equivalent quality gain.

Artifact layout::

    .autodev/tournaments/
      multi-{spec_hash[:8]}/
        branch-0/                # full TournamentArtifactStore lifecycle
          initial_a.md
          pass_NN/...
          incumbent_after_NN.md
          final_output.md
          history.json
        branch-1/...
        branch-2/...
        meta-merge/
          step-0/                # pairwise reduction step (synth+judge)
          step-1/
          ...

Survivor floor: if more than half of branches fail, the multi-branch
runner raises :class:`~errors.TournamentError`. The orchestrator's
fallback path (``plan_phase._walk_multi_branch_for_latest_incumbent``)
recovers from on-disk incumbent_after_NN.md files across all surviving
branches.

Cancellation safety: branches are gathered with
``asyncio.gather(..., return_exceptions=True)`` so one branch's failure
does NOT cancel its siblings — each branch's failure is captured into
``BranchOutcome.error`` and the survivor floor is computed from the
healthy ones.
"""

from __future__ import annotations

import asyncio
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import hashlib
import time

from autologging import get_logger
from errors import TournamentError
from orchestrator.plan_tournament_runner import run_plan_tournament
from state.knowledge import TournamentEvent
from state.paths import autodev_root
from tournament import (
    AdapterLLMClient,
    PassResult,
    PlanContentHandler,
    TournamentArtifactStore,
    aggregate_rankings,
    parse_ranking,
    randomize_for_judge,
)
from tournament.effort import resolve_role_effort
from tournament.prompts import (
    JUDGE_SYSTEM,
    SYNTHESIZER_SYSTEM,
)
from tournament.timeouts import resolve_role_timeout_s


if TYPE_CHECKING:
    from config.schema import BranchConfig
    from orchestrator import Orchestrator


logger = get_logger(__name__)


_TOURNAMENT_ROLES: tuple[str, ...] = ("critic_t", "architect_b", "synthesizer", "judge")


@dataclass
class BranchOutcome:
    """Per-branch result of a multi-branch plan tournament.

    ``success=True`` means ``final_md`` is the branch's converged plan
    markdown. ``success=False`` means the branch raised an exception
    during its tournament (captured into ``error``); ``final_md`` will
    be ``None``. Used by :func:`run_multi_branch_plan_tournament` to
    decide which survivors feed into the meta-merge step.

    v0.17.0 S4: ``metadata`` carries advisory tags from the
    repeated-hypothesis detector (``{"hypothesis_repeat": True}``) and
    future per-branch annotations. Free-form dict so additions are
    backward-compatible.
    """

    branch_index: int
    success: bool
    final_md: str | None
    error: str | None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass
class MultiBranchOutcome:
    """Aggregate result of a multi-branch plan tournament.

    ``branches`` records the per-branch outcomes (including failures, for
    forensics). ``final_md`` is the meta-merged plan markdown derived
    from the survivors. ``meta_history`` is the concatenated
    :class:`PassResult` list across all pairwise meta-merge steps.
    """

    branches: list[BranchOutcome]
    final_md: str
    meta_history: list[PassResult]


def _survivor_floor(n_branches: int) -> int:
    """Minimum number of survivors required for the meta-merge to proceed.

    ``max(2, ceil(N/2))``: at least 2 survivors are always required (so the
    pairwise meta-merge has something to reduce), and majority-survival
    is the secondary floor (``ceil(N/2)`` for odd N, ``N/2`` for even).
    For N=3, the floor is 2; for N=4 or 5, also 2 / 3 respectively.

    For N=1 the multi-branch path is normally not entered (plan_phase
    dispatch guards on ``num_branches > 1``). When invoked directly with
    N=1 the floor would be 2, which is impossible to satisfy, so the
    test surface for direct N=1 calls would always fail. We don't clamp
    here — instead :func:`run_multi_branch_plan_tournament` short-circuits
    on N=1 before the floor check.
    """
    return max(2, math.ceil(n_branches / 2))


async def _run_one_branch(
    orch: "Orchestrator",
    initial_md: str,
    spec: str,
    spec_hash: str,
    branch_index: int,
    branch_seed: int,
    branch_config: "BranchConfig | None" = None,
) -> str:
    """Wrapper around :func:`run_plan_tournament` to keep ``asyncio.gather``
    happy when one branch fails. Re-raises so ``return_exceptions=True``
    can capture the exception without cancelling siblings.

    v0.14.0: optional ``branch_config`` is threaded into the per-branch
    tournament runner. ``None`` preserves homogeneous v0.12.0 behavior.
    """
    return await run_plan_tournament(
        orch,
        initial_md,
        spec,
        spec_hash,
        branch_index=branch_index,
        branch_seed=branch_seed,
        branch_config=branch_config,
    )


def _build_meta_role_overrides(
    orch: "Orchestrator",
    plan_complexity: str | None,
) -> tuple[
    dict[str, int],
    dict[str, list[str] | None],
    dict[str, int],
    dict[str, str],
]:
    """Build per-role overrides for the meta-merge tournament's LLM client.

    Mirrors :func:`orchestrator.plan_tournament_runner._build_role_overrides`
    but kept local to avoid a private import. The meta-merge uses the same
    set of tournament roles (synthesizer + judge are the load-bearing ones;
    critic_t / architect_b are wired but unused at ``max_rounds=1``).
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


def _resolve_meta_model(orch: "Orchestrator") -> str | None:
    """Resolve the meta-merge model. Mirrors
    :func:`orchestrator.plan_tournament_runner._resolve_tournament_model`
    but kept inline to avoid private imports."""
    spec = orch.registry.get("judge")
    if spec is not None and spec.model:
        return spec.model
    agent_cfg = orch.cfg.agents.get("judge")
    if agent_cfg is not None and agent_cfg.model:
        return agent_cfg.model
    return None


def _stable_seed(*texts: str) -> int:
    """Deterministic 64-bit seed from input texts.

    Used so re-running ``_meta_merge_pairwise`` on the same candidate
    list produces identical judge-shuffle orders. Uses SHA-256 (not
    :func:`hash`) because Python's built-in hash is randomized
    per-process via PYTHONHASHSEED.
    """
    h = hashlib.sha256()
    for t in texts:
        h.update(t[:64].encode("utf-8"))
        h.update(b"\x00")
    return int(h.hexdigest()[:16], 16)


async def _run_meta_merge_step(
    orch: "Orchestrator",
    handler: PlanContentHandler,
    client: AdapterLLMClient,
    spec: str,
    spec_hash: str,
    a_md: str,
    b_md: str,
    step_idx: int,
    num_judges: int,
    judge_model: str | None,
) -> tuple[str, PassResult]:
    """One pairwise meta-merge step: synth(A, B) → AB; judges rank A/B/AB.

    Unlike a full Tournament[T] pass (which runs CRITIC → ARCHITECT_B →
    SYNTH → JUDGE), the meta-merge step accepts ``a_md`` and ``b_md`` as
    pre-existing candidates (the per-branch winners) and only runs:

      1. SYNTHESIZER over (a_md, b_md) → produces AB
      2. N parallel JUDGES rank A/B/AB
      3. Borda aggregation picks the winner

    No CRITIC and no ARCHITECT_B because both candidates already came
    from a full tournament run — re-criticising them risks regressing
    quality. This is the "synthesizer-only meta-merge" contract from
    the v0.12.0 plan.

    Artifact layout::

        {artifact_root}/meta-merge/step-{step_idx}/
          version_a.md
          version_b.md
          version_ab.md
          synth_meta.json
          judges/<i>_order.json + <i>_response.json
          result.json

    Reuses :class:`TournamentArtifactStore` for atomic writes and resume
    correlation but bypasses :class:`Tournament.run_pass` because we
    don't want CRITIC/ARCHITECT_B in the loop.

    Args:
        orch: Orchestrator (for plugin_registry / adapter).
        handler: Already-instantiated :class:`PlanContentHandler`.
        client: Already-instantiated :class:`AdapterLLMClient`.
        spec: User intent (task_prompt).
        spec_hash: For artifact dir derivation.
        a_md: Left candidate (the prior meta-merge result, or candidates[0]).
        b_md: Right candidate (the next branch winner to fold in).
        step_idx: 0-indexed step number, used for the artifact subdir name.
        num_judges: Judge cohort size for this step.
        judge_model: Model identifier for judges (already auto-disable-resolved).

    Returns:
        ``(merged_md, pass_result)`` — the chosen incumbent (A, B, or AB)
        as markdown, and a :class:`PassResult` recording the Borda outcome
        and judge details for forensics.
    """
    t0 = time.time()
    artifact_root = (
        autodev_root(orch.cwd)
        / "tournaments"
        / f"multi-{spec_hash[:8]}"
        / "meta-merge"
    )
    step_dir = artifact_root / f"step-{step_idx}"
    store = TournamentArtifactStore(step_dir)

    # Persist inputs for forensics.
    store.write_version_a(pass_num=1, version_a_md=a_md)
    store.write_version_b(pass_num=1, version_b_md=b_md)

    # Deterministic RNG for this step.
    rng = random.Random(_stable_seed(a_md, b_md, str(step_idx)))

    # 1. SYNTHESIZER — coin-flip X/Y so synth has no positional bias.
    if rng.random() < 0.5:
        v_x, v_y = a_md, b_md
        synth_meta = {"x_label": "A", "y_label": "B"}
    else:
        v_x, v_y = b_md, a_md
        synth_meta = {"x_label": "B", "y_label": "A"}
    synth_user = handler.render_for_synthesizer(spec, v_x, v_y)
    synth_text = await client.call(
        system=SYNTHESIZER_SYSTEM,
        user=synth_user,
        role="synthesizer",
        model=judge_model,
    )
    v_ab = handler.parse_synthesis(synth_text, a_md, b_md)
    store.write_synthesis(pass_num=1, version_ab_md=v_ab, synth_meta=synth_meta)

    # 2. N parallel judges. Reuse the Tournament's randomize_for_judge
    # helper so the inverse-mapping logic is identical.
    judge_coros = []
    judge_orders: list[dict[int, str]] = []
    for j_idx in range(num_judges):
        order = randomize_for_judge(a_md, b_md, v_ab, rng)
        judge_orders.append(order)
        store.write_judge_order(pass_num=1, judge_index=j_idx, order=order)
        judge_user = handler.render_for_judge(spec, a_md, b_md, v_ab, order)
        judge_coros.append(
            client.call(
                system=JUDGE_SYSTEM,
                user=judge_user,
                role="judge",
                model=judge_model,
            )
        )
    judge_responses = await asyncio.gather(*judge_coros, return_exceptions=True)

    # Parse rankings + persist response artifacts.
    rankings: list[list[str] | None] = []
    judge_details: list[dict] = []
    for j_idx, (resp, order) in enumerate(zip(judge_responses, judge_orders)):
        if isinstance(resp, BaseException):
            store.write_judge_response(
                pass_num=1,
                judge_index=j_idx,
                response={"raw": "", "ranking": None, "error": str(resp)},
            )
            rankings.append(None)
            judge_details.append(
                {"error": str(resp), "order": {str(k): v for k, v in order.items()}}
            )
            continue
        raw_ranking = parse_ranking(resp, "123")
        store.write_judge_response(
            pass_num=1,
            judge_index=j_idx,
            response={"raw": resp, "ranking": raw_ranking, "error": None},
        )
        if raw_ranking is None:
            rankings.append(None)
            judge_details.append(
                {
                    "ranking": None,
                    "order": {str(k): v for k, v in order.items()},
                    "raw_response": resp,
                }
            )
        else:
            mapped = [order.get(int(r), r) for r in raw_ranking]
            rankings.append(mapped)
            judge_details.append(
                {
                    "ranking": mapped,
                    "order": {str(k): v for k, v in order.items()},
                    "raw_response": resp,
                }
            )

    # 3. Borda aggregation; conservative tiebreak to "A" (incumbent).
    raw_winner, scores, valid_judges = aggregate_rankings(
        rankings, labels=["A", "B", "AB"], tiebreak_winner="A"
    )

    winner_md_map = {"A": a_md, "B": b_md, "AB": v_ab}
    chosen_md = winner_md_map[raw_winner]
    elapsed = time.time() - t0

    pass_result = PassResult(
        pass_num=1,
        winner=raw_winner,  # type: ignore[arg-type]
        scores=scores,
        valid_judges=valid_judges,
        elapsed_s=round(elapsed, 3),
        judge_details=judge_details,
        incumbent_hash_before=handler.hash(a_md),
        incumbent_hash_after=handler.hash(chosen_md),
        meta={"meta_merge_step": step_idx, "effective_winner": raw_winner},
    )
    store.write_pass_result(pass_num=1, result=pass_result)
    # Final marker for the step (mirrors Tournament.run's final write).
    store.write_final(chosen_md, [pass_result])

    logger.info(
        "multi_branch.meta_merge_step_done",
        step=step_idx,
        winner=raw_winner,
        scores=scores,
        valid_judges=valid_judges,
    )
    return chosen_md, pass_result


async def _meta_merge_pairwise(
    orch: "Orchestrator",
    candidates: list[str],
    spec: str,
    spec_hash: str,
) -> tuple[str, list[PassResult]]:
    """Pairwise reduction: ``synth(c0, c1) -> m1; synth(m1, c2) -> m2; ...``.

    Each step (:func:`_run_meta_merge_step`) is a synthesizer-only merge:
    no CRITIC, no ARCHITECT_B — the two inputs are already converged
    plan markdowns from full per-branch tournaments. Synthesizing,
    judging, and Borda-picking is the entirety of the merge contract.

    Edge cases:
        - ``len(candidates) == 0``: programmer error; raises ValueError.
        - ``len(candidates) == 1``: no meta-merge needed; returns the
          sole survivor with empty history. This handles the "1 survivor
          slipped past the floor" path that should never trigger because
          ``_survivor_floor() >= 2``, but kept for defensive correctness.
        - ``len(candidates) >= 2``: pairwise reduce left-to-right.

    Determinism:
        - Each step's RNG is seeded via :func:`_stable_seed` over the
          truncated candidate texts.
        - Re-running with the same survivors produces identical
          synthesizer / judge orders.

    Args:
        orch: Orchestrator carrying adapter, cfg, registry.
        candidates: List of plan markdown strings (the per-branch winners).
        spec: User intent string (forwarded as ``task_prompt``).
        spec_hash: Spec hash, used for the meta-merge artifact dir name.

    Returns:
        ``(meta_final_md, meta_history)``. ``meta_history`` is the list
        of :class:`PassResult` records (one per pairwise step) for
        forensics + ledger payloads.
    """
    if len(candidates) == 0:
        raise ValueError("_meta_merge_pairwise: no candidates to merge")
    if len(candidates) == 1:
        # No meta-merge needed; sole survivor passes through unchanged.
        logger.info(
            "multi_branch.meta_merge_single_survivor",
            note="returning sole survivor unchanged",
        )
        return candidates[0], []

    # Build LLM client + role overrides once.
    plan_complexity: str | None = None  # complexity-aware overrides aren't
    # meaningful for meta-merge (the candidates are already converged
    # per-branch winners).
    role_max_turns, role_allowed_tools, role_timeout_s, role_effort = (
        _build_meta_role_overrides(orch, plan_complexity)
    )
    client = AdapterLLMClient(
        orch.adapter,
        cwd=orch.cwd,
        role_max_turns=role_max_turns,
        role_allowed_tools=role_allowed_tools,
        role_effort=role_effort,
        role_timeout_s=role_timeout_s,
    )

    plan_cfg = orch.cfg.tournaments.plan
    judge_model = _resolve_meta_model(orch)

    artifact_root = (
        autodev_root(orch.cwd)
        / "tournaments"
        / f"multi-{spec_hash[:8]}"
        / "meta-merge"
    )
    artifact_root.mkdir(parents=True, exist_ok=True)

    handler = PlanContentHandler()
    incumbent = candidates[0]
    history: list[PassResult] = []
    n_steps = len(candidates) - 1
    for step_idx in range(n_steps):
        next_candidate = candidates[step_idx + 1]
        merged_md, pass_result = await _run_meta_merge_step(
            orch=orch,
            handler=handler,
            client=client,
            spec=spec,
            spec_hash=spec_hash,
            a_md=incumbent,
            b_md=next_candidate,
            step_idx=step_idx,
            num_judges=plan_cfg.num_judges,
            judge_model=judge_model,
        )
        incumbent = merged_md
        history.append(pass_result)

    return incumbent, history


async def run_multi_branch_plan_tournament(
    orch: "Orchestrator",
    initial_md: str,
    spec: str,
    spec_hash: str,
    n_branches: int,
    branch_configs: "list[BranchConfig] | None" = None,
) -> MultiBranchOutcome:
    """Run ``n_branches`` parallel plan tournaments, then meta-merge survivors.

    Each branch ``i`` (0-indexed) seeds its RNG from
    ``int(spec_hash, 16) + i`` and writes artifacts to
    ``tournaments/multi-{spec_hash[:8]}/branch-{i}/``. After all branches
    return (or raise), survivors are the ones with ``success=True``.

    Survivor floor: ``max(2, ceil(N/2))``. If fewer than that many
    branches succeed, raises :class:`TournamentError`. With the default
    ``n_branches=3``, the floor is 2 — at most 1 branch may fail before
    falling back to the salvage path.

    On success, runs the meta-merge pairwise reduction over the
    survivors via :func:`_meta_merge_pairwise`. Appends a ledger
    breadcrumb ``multi_branch_plan_tournament_complete`` (commit 11)
    at the end.

    Args:
        orch: Orchestrator with adapter, cfg, registry, plan_manager.
        initial_md: Architect's draft plan markdown.
        spec: User intent (task_prompt).
        spec_hash: 16-hex-char digest of the spec.
        n_branches: Number of parallel branches (must be ≥1).

    Returns:
        :class:`MultiBranchOutcome` with per-branch outcomes and the
        meta-merged final markdown.

    Raises:
        TournamentError: when fewer than the survivor floor of branches
            succeeded; the caller should fall through to the v0.6.0
            salvage path.
        ValueError: when ``n_branches < 1`` (caller misuse).
    """
    if n_branches < 1:
        raise ValueError(f"n_branches must be ≥1, got {n_branches}")

    # v0.14.0: validate branch_configs alignment up-front.
    if branch_configs is not None and len(branch_configs) != n_branches:
        raise ValueError(
            f"len(branch_configs) ({len(branch_configs)}) must equal "
            f"n_branches ({n_branches}) — exact 1:1 correspondence required"
        )

    # N=1 short-circuit: the multi-branch path is normally guarded by
    # ``num_branches > 1`` in plan_phase dispatch, but if a caller does
    # invoke this with N=1 we run a single branch with branch_index=0
    # and skip the survivor-floor check (which requires ≥2). This keeps
    # the API consistent (always returns a MultiBranchOutcome) for unit
    # callers, even though production callers gate on ``num_branches > 1``.
    if n_branches == 1:
        only_seed = int(spec_hash, 16)
        only_branch_config = (
            branch_configs[0] if branch_configs is not None else None
        )
        try:
            only_md = await _run_one_branch(
                orch, initial_md, spec, spec_hash,
                branch_index=0, branch_seed=only_seed,
                branch_config=only_branch_config,
            )
            outcome = MultiBranchOutcome(
                branches=[
                    BranchOutcome(branch_index=0, success=True, final_md=only_md, error=None)
                ],
                final_md=only_md,
                meta_history=[],
            )
            return outcome
        except BaseException as exc:  # noqa: BLE001 — re-raise as TournamentError
            raise TournamentError(
                f"single-branch (N=1) multi-branch run failed: {exc}"
            ) from exc

    # Audit-trail breadcrumb at start.
    base_seed = int(spec_hash, 16)
    branch_seeds = [base_seed + i for i in range(n_branches)]
    await orch.plan_manager.ledger_append(
        op="multi_branch_plan_tournament_start",
        payload={
            "spec_hash": spec_hash,
            "n_branches": n_branches,
            "branch_seeds": [str(s) for s in branch_seeds],  # JSON-safe
        },
    )

    logger.info(
        "multi_branch.start",
        spec_hash=spec_hash,
        n_branches=n_branches,
    )

    # v0.17.0 S4: pre-gather repeated-hypothesis advisory check. For each
    # branch, derive a hypothesis from ``branch_config.family`` (when set)
    # or fall back to the first 500 chars of ``initial_md``. Tag branches
    # whose hypothesis matches a recent (≤14d) discard so downstream
    # forensics + the future plateau detector can weigh structurally
    # repeated approaches. Advisory ONLY — does not skip branches.
    repeat_tags: list[bool] = [False] * n_branches
    if orch.cfg.repeated_hypothesis_threshold > 0:
        from orchestrator.repeat_detector import RepeatedHypothesisDetector

        detector = RepeatedHypothesisDetector(orch.knowledge)
        threshold = orch.cfg.repeated_hypothesis_threshold
        for i in range(n_branches):
            bc = branch_configs[i] if branch_configs is not None else None
            family = bc.family if bc is not None else None
            hypothesis = family if family else initial_md[:500]
            try:
                is_rep = await detector.is_repeat(
                    hypothesis, family=family, threshold=threshold
                )
            except Exception as exc:  # noqa: BLE001 — advisory: never block
                logger.warning(
                    "multi_branch.repeat_check_failed",
                    branch_index=i,
                    error=str(exc),
                )
                is_rep = False
            if is_rep:
                repeat_tags[i] = True
                logger.warning(
                    "multi_branch.hypothesis_repeat",
                    branch_index=i,
                    family=family or "<none>",
                )
                await orch.plan_manager.ledger_append(
                    op="hypothesis_repeat_detected",
                    payload={
                        "spec_hash": spec_hash,
                        "branch_index": i,
                        "family": family,
                    },
                )

    # Step 1: gather N parallel branches with return_exceptions so a single
    # branch failure does NOT cancel siblings.
    coros = [
        _run_one_branch(
            orch,
            initial_md,
            spec,
            spec_hash,
            branch_index=i,
            branch_seed=branch_seeds[i],
            branch_config=(
                branch_configs[i] if branch_configs is not None else None
            ),
        )
        for i in range(n_branches)
    ]
    raw_results = await asyncio.gather(*coros, return_exceptions=True)

    branches: list[BranchOutcome] = []
    for i, r in enumerate(raw_results):
        # v0.17.0 S4: thread the advisory repeat tag into per-branch metadata.
        meta: dict[str, object] = {}
        if repeat_tags[i]:
            meta["hypothesis_repeat"] = True
        if isinstance(r, BaseException):
            branches.append(
                BranchOutcome(
                    branch_index=i,
                    success=False,
                    final_md=None,
                    error=str(r),
                    metadata=meta,
                )
            )
            logger.warning(
                "multi_branch.branch_failed",
                branch_index=i,
                error=str(r),
            )
        else:
            branches.append(
                BranchOutcome(
                    branch_index=i,
                    success=True,
                    final_md=r,
                    error=None,
                    metadata=meta,
                )
            )

    survivors = [b for b in branches if b.success]
    floor = _survivor_floor(n_branches)
    if len(survivors) < floor:
        logger.warning(
            "multi_branch.under_floor",
            survivors=len(survivors),
            floor=floor,
            n_branches=n_branches,
        )
        raise TournamentError(
            f"only {len(survivors)} of {n_branches} branches succeeded; "
            f"survivor floor is {floor}"
        )

    logger.info(
        "multi_branch.survivors",
        survivors=len(survivors),
        of=n_branches,
        floor=floor,
    )

    # Step 2: pairwise meta-merge over the survivor markdowns.
    survivor_mds = [b.final_md for b in survivors if b.final_md is not None]
    final_md, meta_history = await _meta_merge_pairwise(
        orch, survivor_mds, spec, spec_hash
    )

    # Audit-trail breadcrumbs at end.
    await orch.plan_manager.ledger_append(
        op="multi_branch_meta_merge_complete",
        payload={
            "spec_hash": spec_hash,
            "n_survivors": len(survivors),
            "n_steps": max(0, len(survivor_mds) - 1),
            "meta_passes": len(meta_history),
        },
    )
    await orch.plan_manager.ledger_append(
        op="multi_branch_plan_tournament_complete",
        payload={
            "spec_hash": spec_hash,
            "n_branches": n_branches,
            "n_survivors": len(survivors),
            "final_hash": _short_hash(final_md),
        },
    )

    # v0.15.0: emit cross-run lessons from the meta-merge boundary.
    # ``winner_promoted`` for the final markdown and one ``discard`` per
    # failed (raised) branch — the per-branch successful tournaments
    # already emit their own per-pass lessons through
    # ``plan_tournament_runner._emit_plan_tournament_lessons``. Errors
    # are swallowed so a knowledge failure can't sink the dispatch.
    try:
        await _emit_meta_merge_lessons(
            orch,
            spec_hash=spec_hash,
            n_branches=n_branches,
            survivors=survivors,
            failed=[b for b in branches if not b.success],
            final_md=final_md,
            meta_passes=len(meta_history),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "multi_branch.lessons_emit_failed",
            spec_hash=spec_hash,
            error=str(exc),
        )

    logger.info(
        "multi_branch.done",
        spec_hash=spec_hash,
        n_survivors=len(survivors),
        meta_passes=len(meta_history),
    )

    return MultiBranchOutcome(
        branches=branches,
        final_md=final_md,
        meta_history=meta_history,
    )


async def _emit_meta_merge_lessons(
    orch: "Orchestrator",
    *,
    spec_hash: str,
    n_branches: int,
    survivors: list[BranchOutcome],
    failed: list[BranchOutcome],
    final_md: str,
    meta_passes: int,
) -> None:
    """Emit cross-run lessons from a completed multi-branch meta-merge.

    For each failed branch, one ``discard`` lesson tagged with the branch
    index + the captured error so future runs see *why* the branch
    failed (e.g. a recurring crash signature is a strong hint to skip
    that lane). After all discards, one ``winner_promoted`` lesson
    summarizing the meta-merged final.

    Errors propagate to the caller (which logs + swallows).
    """
    family = "multi-branch-meta-merge"

    for f in failed:
        evidence = (
            f"spec_hash={spec_hash} branch={f.branch_index} "
            f"of={n_branches} error={f.error or '<unknown>'}"
        )
        await orch.knowledge.record_tournament_event(
            TournamentEvent(
                event_type="discard",
                family=family,
                hypothesis=(
                    f"branch {f.branch_index} of {n_branches} failed during "
                    f"per-branch plan tournament"
                ),
                evidence=evidence,
                rollback_reason="branch-tournament-raised",
            )
        )

    final_fingerprint = (
        f"spec_hash={spec_hash} n_survivors={len(survivors)} of={n_branches} "
        f"meta_passes={meta_passes} final_hash={_short_hash(final_md)} "
        f"line_count={len(final_md.splitlines())}"
    )
    await orch.knowledge.record_tournament_event(
        TournamentEvent(
            event_type="winner_promoted",
            family=family,
            hypothesis=(
                f"meta-merge over {len(survivors)} survivors produced final "
                f"plan; spec_hash={spec_hash}"
            ),
            evidence=final_fingerprint,
            next_action_hint=(
                "future multi-branch runs on this spec should prefer the "
                "lane(s) that survived"
            ),
        )
    )


def _short_hash(text: str) -> str:
    """Return a 16-hex-char SHA-256 prefix — same convention as elsewhere."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Multi-branch resume + salvage helpers (commits 8 + 10/12)
# ---------------------------------------------------------------------------


def multi_branch_parent_dir(cwd: Path, spec_hash: str) -> Path:
    """Return the parent directory for a multi-branch run on ``spec_hash``.

    ``.autodev/tournaments/multi-{spec_hash[:8]}/``. Used by the salvage
    walker to enumerate per-branch artifact dirs without hard-coding the
    layout in multiple call sites.
    """
    return autodev_root(cwd) / "tournaments" / f"multi-{spec_hash[:8]}"


__all__ = [
    "BranchOutcome",
    "MultiBranchOutcome",
    "_meta_merge_pairwise",
    "_survivor_floor",
    "multi_branch_parent_dir",
    "run_multi_branch_plan_tournament",
]
