"""Pydantic v2 schema for `.autodev/config.json`."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


REQUIRED_AGENT_ROLES: tuple[str, ...] = (
    "architect",
    "explorer",
    "domain_expert",
    "developer",
    "reviewer",
    "test_engineer",
    "critic_sounding_board",
    "critic_drift_verifier",
    "docs",
    "designer",
    "critic_t",
    "architect_b",
    "synthesizer",
    "judge",
)


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str | None = None
    disabled: bool = False
    max_turns: int | None = None  # None = use role default
    # Per-role override for Claude Code's ``--effort`` test-time-compute
    # flag. ``None`` = inherit (let the orchestrator's effort resolver
    # derive a value from plan + user complexity, or fall back to the
    # user-global default in ``~/.claude/settings.json``). Validated at the
    # config layer; the adapter accepts any string for forward-compat.
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None


class TournamentPhaseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    num_judges: int
    convergence_k: int
    max_rounds: int
    # Optional runaway detector: terminate early when the per-pass Borda
    # scores barely change across ``score_stability_window`` consecutive
    # passes (sum of |Δscore| across A/B/AB is ≤ ``score_stability_max_delta``).
    # Both fields default to ``None`` — feature off — so existing config files
    # validate unchanged.
    score_stability_window: int | None = None
    score_stability_max_delta: int | None = None
    # Optional winner-stability detector (v0.6.0 / Issue 4). When set,
    # terminates early if the trailing ``winner_stability_window`` passes all
    # share the same non-A ``effective_winner`` (the QNX runaway pattern of
    # `[AB, AB, AB]`). Defaults to ``None`` so legacy on-disk configs validate.
    winner_stability_window: int | None = None
    # Optional maximum line-growth ratio for AB winners (v0.6.2 / Issue 5B).
    # When set, an AB Borda winner whose markdown exceeds
    # ``max_plan_lines_growth_ratio * len(incumbent.splitlines())`` lines is
    # demoted to the next-best Borda winner — the verbose synthesizer no
    # longer wins by sheer volume. Defaults to ``None`` (off) so legacy
    # configs validate; the plan-tournament default ships with ``1.5``.
    # Impl tournaments leave this ``None`` because impl artifacts aren't
    # line-counted plan markdown.
    max_plan_lines_growth_ratio: float | None = None
    # Optional escalation knob for the judge ensemble on complex plans
    # (v0.7.0 / Issue 5C). When the architect's ``COMPLEXITY:`` directive
    # resolves to ``"complex"`` and this field is non-None, the plan
    # tournament substitutes ``num_judges`` with this value for that run.
    # Adopts autoreason's "7 judges → ~3× faster convergence" finding —
    # opt-in cost (~40% more judge calls per complex plan) gated on a
    # complexity classification the architect already emits. Defaults to
    # ``None`` so legacy configs validate; the plan-tournament default
    # ships with ``7``. Impl tournaments leave this ``None`` because impl
    # complexity isn't extracted from a plan markdown.
    complex_plan_num_judges_override: int | None = None


def _default_phase_review_cfg() -> "TournamentPhaseConfig":
    """Default-on phase-review config used when an existing ``config.json``
    omits the new v0.9.0 field.

    Defaults: enabled, single-pass (max_rounds=2), 3 judges. Single-pass
    keeps cost contained; 3 judges balances signal vs. cost. The
    score / winner-stability detectors are not configured because
    ``max_rounds=2`` is too small for the windows to fire.
    """
    return TournamentPhaseConfig(
        enabled=True,
        num_judges=3,
        convergence_k=1,
        max_rounds=2,
        score_stability_window=None,
        score_stability_max_delta=None,
        winner_stability_window=None,
        max_plan_lines_growth_ratio=None,
        complex_plan_num_judges_override=None,
    )


class TournamentsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: TournamentPhaseConfig
    impl: TournamentPhaseConfig
    # v0.9.0: per-phase code review tournament config. Default-on per the
    # user-locked-in design. The ``Field(default_factory=...)`` ensures
    # legacy on-disk configs (written by v0.8.0 or earlier) without this
    # field still validate — the factory inlines the v0.9.0 defaults.
    phase_review: TournamentPhaseConfig = Field(
        default_factory=_default_phase_review_cfg
    )
    max_parallel_subprocesses: int = 3
    auto_disable_for_models: list[str] = Field(default_factory=lambda: ["opus"])


class QAGatesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    syntax_check: bool = True
    lint: bool = True
    build_check: bool = True
    test_runner: bool = True
    secretscan: bool = True
    # These two fields are NOT dispatched by _run_qa_gates. They are consumed
    # exclusively by agent prompts (e.g. architect.md) to drive security-tier
    # routing decisions at planning time. Dispatching them as actual gates is
    # planned in ADR-008 (see line 104 of this file).
    sast_scan: bool = False
    mutation_test: bool = False


class GuardrailsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Upper bound on agent round-trips per task (enforced in pre_invocation).
    max_invocations_per_task: int = 60
    # Upper bound on cumulative tool calls per task. Requires stream-json
    # parsing (Phase 3 functionality) to be fully enforced; currently
    # tool_calls are populated only when stream-json is used.
    max_tool_calls_per_task: int = 60
    max_duration_s_per_task: int = 900
    max_diff_bytes: int = 5_242_880
    cost_budget_usd_per_plan: float | None = None


class HiveConfig(BaseModel):
    """File-level settings for the hive (cross-project) knowledge tier.

    Governs the *on-disk* location and a master switch. Behavioral tuning
    (ranking, dedup, denylist, etc.) lives on :class:`KnowledgeConfig`.

    ``HiveConfig.enabled`` is the master switch for the hive file itself
    (write + read of ``shared-learnings.jsonl``). The knowledge store also
    honors :attr:`KnowledgeConfig.hive_enabled` for symmetry with the
    swarm-level toggle; both must be true for hive I/O to occur.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    path: Path


class KnowledgeConfig(BaseModel):
    """Behavioral config for the two-tier knowledge system (Phase 9).

    Separate from :class:`HiveConfig` — the latter holds path + master switch;
    this model holds dedup thresholds, ranking toggles, capacity caps, and
    injection policy.

    Hive enablement resolution: ``HiveConfig.enabled and KnowledgeConfig.hive_enabled``.
    Keeping both lets operators disable the hive file entirely (HiveConfig.enabled=false)
    OR leave the file in place but skip hive reads/writes for a particular project
    (KnowledgeConfig.hive_enabled=false).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    swarm_max_entries: int = 100
    hive_max_entries: int = 200
    dedup_threshold: float = 0.6
    max_inject_count: int = 5
    hive_enabled: bool = True
    promotion_min_confirmations: int = 3
    promotion_min_confidence: float = 0.7
    denylist_roles: list[str] = Field(
        default_factory=lambda: [
            "explorer",
            "judge",
            "critic_t",
            "architect_b",
            "synthesizer",
        ]
    )


class AutodevConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0.0"] = "1.0.0"
    platform: Literal["claude_code", "cursor", "inline", "auto"] = "auto"
    agents: dict[str, AgentConfig]
    tournaments: TournamentsConfig
    qa_gates: QAGatesConfig = Field(default_factory=QAGatesConfig)
    qa_retry_limit: int = 3
    # User-declared task-complexity bucket. Drives the architect's effort
    # floor (``xhigh`` for {low, medium, high}, ``max`` for ``max``) and is
    # combined with the parsed ``Plan.complexity`` to derive per-role effort
    # for downstream agents. Defaults to ``"medium"`` so existing on-disk
    # configs validate without migration. Distinct from ``Plan.complexity``
    # which uses {simple, medium, complex}.
    user_complexity: Literal["low", "medium", "high", "max"] = "medium"
    guardrails: GuardrailsConfig = Field(default_factory=GuardrailsConfig)
    hive: HiveConfig
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)

    def require_all_roles(self) -> None:
        """Raise ValueError if any required role is missing from `agents`."""
        missing = [r for r in REQUIRED_AGENT_ROLES if r not in self.agents]
        if missing:
            raise ValueError(f"missing required agent roles: {missing}")
