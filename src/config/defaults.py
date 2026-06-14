"""Default `.autodev/config.json` content."""

from __future__ import annotations

from pathlib import Path

from config.schema import (
    AgentConfig,
    AutodevConfig,
    FramingPhaseConfig,
    GuardrailsConfig,
    HiveConfig,
    QAGatesConfig,
    TournamentPhaseConfig,
    TournamentsConfig,
)


_AGENT_MODEL_DEFAULTS: dict[str, str | None] = {
    "architect": None,
    "explorer": None,
    "domain_expert": None,
    "developer": None,
    "reviewer": None,
    "test_engineer": None,
    "critic_sounding_board": None,
    "critic_drift_verifier": None,
    "docs": None,
    "designer": None,
    "critic_t": None,
    "architect_b": None,
    "synthesizer": None,
    "judge": None,
    # ADR-0044: unregistered specialist roles. NOT in REQUIRED_AGENT_ROLES (so
    # absent from the registry), but cfg.agents needs an entry so the specialist
    # dispatch (_invoke_framing_role) can read model/max-turns for the invocation.
    "framing": None,
    "altitude_judge": None,
}

_AGENT_MAX_TURNS: dict[str, int] = {
    "architect": 5,
    "explorer": 3,
    "domain_expert": 3,
    "developer": 10,
    # v0.31.0 (Phase 1.4): bumped 3 → 5. Reviewers genuinely need more
    # turns on non-trivial diffs (the prior cap was a tail contributor
    # to Hypothesis A — empty result when the turn budget ran out
    # before the model emitted the verdict).
    "reviewer": 5,
    "test_engineer": 5,
    "critic_sounding_board": 3,
    "critic_drift_verifier": 3,
    "docs": 3,
    "designer": 3,
    "critic_t": 1,
    "architect_b": 5,
    "synthesizer": 1,
    "judge": 1,
    "framing": 1,
    "altitude_judge": 1,
}


def resolve_model(model: str | None, role: str, platform: str) -> str:
    """Resolve model based on platform and role.

    Cursor:
    - architect/architect_b: opus (high reasoning, falls back to auto if rate limited)
    - reviewer/judge/critic_*/synthesizer/docs: sonnet (moderate reasoning)
    - explorer/developer/test_engineer: auto (auto-selects best model per-task)

    Claude Code: Uses aliases (opus/sonnet/haiku) that auto-resolve to latest.
    """
    if model is not None:
        return model

    if platform == "cursor":
        if role in ("architect", "architect_b"):
            return "opus"
        if role in (
            "reviewer",
            "judge",
            "critic_t",
            "synthesizer",
            "critic_drift_verifier",
            "docs",
            "designer",
            "domain_expert",
        ):
            return "sonnet"
        return "auto"

    if role == "architect":
        return "opus"
    if role == "explorer":
        return "haiku"
    return "sonnet"


def default_config(platform: str = "auto") -> AutodevConfig:
    """Return the shipped default configuration."""
    agents = {
        name: AgentConfig(
            model=resolve_model(model, name, platform),
            max_turns=_AGENT_MAX_TURNS.get(name, 1),
        )
        for name, model in _AGENT_MODEL_DEFAULTS.items()
    }
    return AutodevConfig(
        schema_version="1.0.0",
        platform="auto",
        agents=agents,
        tournaments=TournamentsConfig(
            plan=TournamentPhaseConfig(
                enabled=True,
                # Bumped 3 -> 5: autoreason finds 7 judges yield ~3x faster
                # convergence than 3. We pick 5 as a cost/quality middle ground.
                num_judges=5,
                convergence_k=2,
                max_rounds=15,
                # Runaway detector: terminate early when per-pass Borda scores
                # are stuck across several consecutive passes. Window=4 is
                # half of max_rounds. v0.6.0 bumped ``max_delta`` 1→2 to fire
                # on the QNX historical trajectory `[(5,10,15)*3, (5,12,13),
                # (5,11,14)]` at pass 5 (window-[P2..P5] total delta = 2).
                score_stability_window=4,
                score_stability_max_delta=2,
                # v0.6.0 / Issue 4: winner-stability detector. Halts when 3
                # consecutive passes share the same non-A effective winner —
                # the QNX runaway pattern of `[AB, AB, AB]` that the score
                # detector cannot catch on a divergent-but-stable trajectory.
                # Combined with ``score_stability_max_delta=2`` both detectors
                # cover both failure modes (stuck-numbers AND stuck-labels).
                winner_stability_window=3,
                # v0.6.2 / Issue 5B: oversize-AB demotion. When the
                # synthesizer's AB candidate exceeds 1.5× the incumbent line
                # count, demote it to the next-best Borda winner so the
                # tournament can't be hijacked by ever-growing merges.
                max_plan_lines_growth_ratio=1.5,
                # v0.7.0 / Issue 5C: escalate to a 7-judge ensemble when
                # the architect classifies the plan as "complex" (autoreason:
                # 7 judges → ~3× faster convergence than 3). Medium / simple
                # plans stay at the cheaper default of 5 judges above.
                complex_plan_num_judges_override=7,
                # v0.12.0: number of independent RNG-seeded tournament
                # branches to run in parallel for the plan phase. ``3`` is
                # the user-locked-in default ("maximum diversity"): three
                # parallel trajectories, each seeded from
                # ``int(spec_hash, 16) + branch_index``, with their final
                # outputs meta-merged via the existing
                # :class:`~tournament.plan_tournament.PlanContentHandler`
                # synthesizer. Cost: 3x LLM call volume per plan-phase
                # (mitigated by v0.10.0's resource probe throttling).
                num_branches=3,
            ),
            impl=TournamentPhaseConfig(
                enabled=True,
                # v0.22.0 Phase 4 (anti-bloat): default impl-tournament
                # cohort is now a 3-judge specialist panel:
                #   * ``judge``           — generic Borda (correctness +
                #                           tests + plan-drift); weight 1.0
                #   * ``judge_explorer``  — anti-slop FINDINGS specialist;
                #                           weight 1.0
                #   * ``minimality_judge`` — minimality specialist;
                #                            weight 0.5 (advisory)
                # The minimality judge is intentionally weighted BELOW
                # correctness — when the two disagree, correctness wins.
                # Weights apply via the existing ``BordaAggregator`` weight
                # path. Operators can override either field in their config
                # to revert to a single ``["judge"]`` cohort.
                num_judges=3,
                convergence_k=1,
                max_rounds=3,
                # max_rounds is small (3); window=2 still permits one normal
                # pass before the detector can latch onto a stuck Borda
                # outcome. ``max_delta`` stays at 1 — bumping to 2 would
                # be unsafe with only 3 rounds total.
                score_stability_window=2,
                score_stability_max_delta=1,
                # Smaller window=2 paired with the small max_rounds=3.
                winner_stability_window=2,
                # Impl tournaments operate on diff bundles, not line-counted
                # plan markdown — leave the line-ratio knob disabled.
                max_plan_lines_growth_ratio=None,
                # v0.7.0 / Issue 5C: impl tournaments don't extract
                # complexity from a plan markdown, so the override stays
                # ``None``. Field is plumbed through for schema symmetry.
                complex_plan_num_judges_override=None,
                # v0.12.0: impl tournament stays single-branch — branch
                # fan-out isn't wired into the impl runner in this release.
                num_branches=1,
                judge_roles=["judge", "judge_explorer", "minimality_judge"],
                judge_role_weights={
                    "judge": 1.0,
                    "judge_explorer": 1.0,
                    "minimality_judge": 0.5,
                },
            ),
            # v0.9.0: per-phase code review tournament. Default-on per the
            # user-locked-in design. Single-pass (``max_rounds=2``) keeps
            # cost contained. 3 judges balances signal vs cost. The
            # stability detectors are unset because ``max_rounds=2`` is
            # too small for the windows to fire (the runner's tournament
            # config plumbs through the ``None``s without harm).
            phase_review=TournamentPhaseConfig(
                enabled=True,
                num_judges=3,
                convergence_k=1,
                max_rounds=2,
                score_stability_window=None,
                score_stability_max_delta=None,
                winner_stability_window=None,
                max_plan_lines_growth_ratio=None,
                complex_plan_num_judges_override=None,
                # v0.12.0: phase_review tournament stays single-branch —
                # branch fan-out is plan-tournament-only in this release.
                num_branches=1,
            ),
            # v0.10.0: default flips 3 → None (auto-resolve via
            # ``runtime.resource_probe.resolve_parallelism`` at tournament
            # startup). Operators can still pin an explicit int in their
            # config to bypass the probe (e.g. on hosts where psutil
            # mis-reports capacity, or to force a known-stable value).
            max_parallel_subprocesses=None,
            # v0.11.0: max parallel execute_phase task workers. ``None``
            # auto-resolves via the same resource_probe at run_execute_phase
            # entry. Distinct from max_parallel_subprocesses (which caps
            # the judge cohort fan-out inside one tournament).
            execute_max_parallel_tasks=None,
            # v0.25.3: top-level fallback flipped to ``[]``. Per-tournament
            # built-in defaults (plan=[], impl=["opus"],
            # phase_review=["opus"]) are applied by the
            # :meth:`TournamentsConfig._resolve_auto_disable` validator
            # whenever a per-tournament slot is left ``None``. Existing
            # on-disk configs that still pin ``["opus"]`` here continue
            # to disable all three tournaments until refreshed.
            auto_disable_for_models=[],
        ),
        qa_gates=QAGatesConfig(),
        qa_retry_limit=3,
        qa_retry_min_interval_s=30.0,
        user_complexity="medium",
        guardrails=GuardrailsConfig(),
        hive=HiveConfig(
            enabled=True,
            path=Path("~/.local/share/autodev/shared-learnings.jsonl"),
        ),
        framing=FramingPhaseConfig(
            enabled=True,
            design_smell_threshold=0.7,
            num_approaches=3,
            require_structural_signal=True,
            altitude_judge_panel_size=3,
            classifier_model=None,
            altitude_judge_model=None,
        ),
    )
