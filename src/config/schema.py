"""Pydantic v2 schema for `.autodev/config.json`."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


class BranchConfig(BaseModel):
    """v0.14.0: per-branch configuration for heterogeneous-model multi-branch
    plan tournaments.

    The default plan-tournament configuration runs N homogeneous branches
    (same model per role across all branches). Setting
    :attr:`TournamentPhaseConfig.branches` to a list of :class:`BranchConfig`
    swaps each branch's per-role model and stamps a divergent
    ``lane`` / ``risk`` / ``family`` tag for forensics + future
    plateau-detection heuristics.

    Fields:

    * ``model_overrides``: ``{role: model_name}`` map. The plan-tournament
      runner consults this first when resolving the per-role model;
      missing roles fall through to the existing :func:`resolve_model`
      path (i.e. ``cfg.agents[role].model`` then registry default).
      Empty dict (default) means "no overrides" — branch behaves like a
      legacy homogeneous branch.
    * ``lane``: divergent-trajectory label.
      Used to suffix the branch's artifact directory
      (``branch-{i}-{lane}/``) and stamped into ledger metadata.
    * ``risk``: relative risk classification (``low`` / ``medium`` /
      ``high``). Advisory only in v0.14.0 — future versions may use it
      to gate higher-risk branches behind a separate cohort.
    * ``family``: free-form tag for plateau detection. Two branches with
      the same family but different lanes are siblings; the future
      v0.14.1+ plateau detector can suppress repeated families.

    All fields have safe defaults so an empty :class:`BranchConfig` is
    well-formed (lane=local-tweak, risk=medium, no overrides).
    """

    model_config = ConfigDict(extra="forbid")

    model_overrides: dict[str, str] = Field(default_factory=dict)
    lane: Literal[
        "distant-scout",
        "local-tweak",
        "architectural",
        "constraint-removal",
        "incumbent-confirmation",
    ] = "local-tweak"
    risk: Literal["low", "medium", "high"] = "medium"
    family: str | None = None

    @field_validator("model_overrides", mode="after")
    @classmethod
    def _validate_role_keys(cls, v: dict[str, str]) -> dict[str, str]:
        """Reject empty model strings (would silently fall through to
        resolve_model). Empty dict is fine; an entry mapping a role to
        ``""`` is almost certainly a config typo."""
        for role, model in v.items():
            if not isinstance(model, str) or not model.strip():
                raise ValueError(
                    f"BranchConfig.model_overrides[{role!r}] must be a non-empty string"
                )
        return v


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
    # v0.12.0: number of independent RNG-seeded tournament branches to run
    # in parallel. ``1`` (default) preserves single-branch behavior. When
    # ``>1``, the plan-tournament dispatch fans out N tournaments (each in
    # its own ``tournaments/multi-{hash}/branch-N/`` artifact dir) and
    # then meta-merges the survivors via the existing
    # :class:`~tournament.plan_tournament.PlanContentHandler` synthesizer.
    # Validation: clamped to [1, 5] — 1 disables fan-out, 5 is a hard
    # ceiling on cost (5× LLM call volume vs. single-branch). Defaults
    # to 1 across plan / impl / phase_review; the plan-tournament default
    # in :mod:`config.defaults` overrides to 3 for "maximum diversity"
    # per the v0.12.0 user-locked-in setting. Impl + phase_review leave
    # ``num_branches=1`` because branch fan-out isn't wired into those
    # runners in this release.
    num_branches: int = Field(default=1, ge=1, le=5)
    # v0.14.0: heterogeneous-model branches. ``None`` (default) preserves
    # v0.12.0 homogeneous behavior — every branch uses the same per-role
    # models. When set to a non-None list, each entry is a
    # :class:`BranchConfig` describing a divergent branch's model overrides
    # / lane / risk / family. The multi-branch dispatcher uses the list's
    # length as the branch fan-out count and threads each entry's model
    # overrides into the per-branch tournament runner. Mutually exclusive
    # with ``num_branches > 1`` (validated below).
    branches: list[BranchConfig] | None = None
    # v0.16.0 promotion-grade ladder toggle. Off by default — opt-in for
    # safety-critical work where a single-pass winner shouldn't auto-
    # promote to incumbent. When True, the tournament loop drives
    # :func:`tournament.promotion.decide` to advance the on-disk grade
    # rung (``dev_best`` → ``repeated`` → ``promotion_eligible``) across
    # consecutive non-A wins. The flag lives on the per-phase config so
    # it can be enabled for the plan tournament (where double-checking
    # incumbent quality matters most) while staying off for the impl
    # tournament (where the cost doubling would be punitive).
    promotion_grade_enabled: bool = False
    # v0.16.0: drift-verifier final-defense gate. Off by default so a
    # missing ``critic_drift_verifier`` agent spec or unstubbed test
    # adapter doesn't surprise legacy callers. When True (and used on
    # ``cfg.tournaments.phase_review``), :func:`run_phase_review_tournament`
    # invokes :func:`orchestrator.drift_verifier.run_drift_verifier`
    # after an A-winner outcome and may flip ``accept_phase`` to False.
    drift_verifier_enabled: bool = False

    @model_validator(mode="after")
    def _validate_branches(self) -> "TournamentPhaseConfig":
        """Enforce the v0.14.0 ``branches`` invariants.

        * Empty list ``[]`` is rejected — use ``None`` to disable.
        * Length is clamped to [1, 5] (matches ``num_branches`` ceiling).
        * Non-None branches AND ``num_branches > 1`` is rejected — the
          two are mutually exclusive paths to multi-branch fan-out.
          ``num_branches=1`` (the default) is fine alongside ``branches``;
          the dispatcher derives the actual count from ``len(branches)``.
        """
        if self.branches is not None:
            if len(self.branches) == 0:
                raise ValueError(
                    "TournamentPhaseConfig.branches must be None or a "
                    "non-empty list (use None to disable hetero-models)"
                )
            if len(self.branches) > 5:
                raise ValueError(
                    f"TournamentPhaseConfig.branches has "
                    f"{len(self.branches)} entries, max is 5"
                )
            if self.num_branches > 1:
                raise ValueError(
                    "TournamentPhaseConfig: ``branches`` and "
                    "``num_branches > 1`` are mutually exclusive — "
                    f"got num_branches={self.num_branches} with "
                    f"{len(self.branches)} branch entries"
                )
        return self


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
        num_branches=1,
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
    # v0.10.0: widened ``int`` → ``int | None``. ``None`` means
    # "auto-resolve at tournament startup via
    # :func:`runtime.resource_probe.resolve_parallelism`" — a host-aware
    # value derived from CPU count, available memory, and judge cohort
    # size. An explicit int still passes through unchanged for
    # backward-compat with pre-v0.10.0 configs (and as an escape hatch
    # for hosts where psutil mis-reports capacity).
    max_parallel_subprocesses: int | None = None
    # v0.11.0: max number of execute_phase task workers running in
    # parallel. ``None`` (default) means "auto-resolve via
    # :func:`runtime.resource_probe.resolve_parallelism` with
    # ``role_mix='execute'``". An explicit int bypasses the probe.
    # This is distinct from ``max_parallel_subprocesses`` — that field
    # caps judge cohort fan-out inside one tournament; this field caps
    # task-worker fan-out inside execute_phase. Both can be set
    # independently.
    execute_max_parallel_tasks: int | None = None
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
