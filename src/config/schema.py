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
    # v0.23.0 C4: opt-out for the plan-tournament huge-repo fast-path.
    # When ``False`` (default) and ``runtime.repo_probe.is_huge`` is True,
    # ``orchestrator.plan_phase`` falls back to a single-branch tournament
    # so Unity-scale repos don't burn 80 min on the multi-branch dispatch.
    # Set ``True`` to keep the configured branch count even on huge repos
    # (operators with bigger compute budgets / parallelism).
    huge_repo_overrides_disabled: bool = False
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
    # v0.19.0 C1: holdout-set evaluation. When True and a winner reaches
    # the ``repeated`` ladder rung, the tournament invokes the holdout
    # runner against the baseline-commit ``tests/`` snapshot before
    # promoting. Failure → ``no_change`` (winner stays at ``repeated``);
    # success → ``promote_to_eligible`` as before.
    holdout_evaluation_enabled: bool = False
    # v0.16.0: drift-verifier final-defense gate. v0.17.0 flips the default
    # ON now that ``tests/stub_adapter.py`` returns a parser-compatible
    # ``VERDICT: APPROVED`` for ``critic_drift_verifier`` by default —
    # legacy callers / unstubbed adapters no longer trip on the gate.
    # Used on ``cfg.tournaments.phase_review``,
    # :func:`run_phase_review_tournament` invokes
    # :func:`orchestrator.drift_verifier.run_drift_verifier` after an
    # A-winner outcome and may flip ``accept_phase`` to False on drift.
    drift_verifier_enabled: bool = True
    # v0.17.0 S3: anti-slop Explorer specialist judge. When True, the
    # tournament dispatches an additional Explorer judge alongside the
    # standard judge ensemble; its ``FINDINGS:`` block is parsed via
    # :func:`tournament.core.extract_explorer_findings` and emitted as
    # ``discard``-grade lessons (confidence 0.6) for forensics + future
    # passes. Default False — opt-in because Explorer uses an extra LLM
    # call per pass and is most useful on long-form impl outputs where
    # slop / hallucinated APIs are the dominant failure mode.
    explorer_enabled: bool = False
    # v0.18.0 C1: pluggable judge-aggregation strategy. ``"borda"`` (default)
    # uses the legacy Borda count and is byte-identical to v0.17.0
    # behavior. ``"veto"`` switches to the council/veto policy implemented
    # in :class:`tournament.voting.VetoAggregator` — any judge ranking a
    # candidate last vetoes that candidate; surviving candidates fall
    # through to a Borda tally. Currently consumed by the impl-tournament
    # runner only; plan + phase_review continue to use the default.
    voting_strategy: Literal["borda", "veto"] = "borda"
    # v0.18.0 C3: optional list of specialist judge roles. ``None``
    # (default) preserves the legacy ``["judge"] * num_judges`` cohort.
    # When set (e.g. ``["critic", "reviewer", "test_engineer",
    # "domain_expert", "explorer"]``), each entry becomes a judge of that
    # role. The list length wins over ``num_judges`` — ``num_judges`` is
    # derived as ``len(judge_roles)`` when the list is set.
    judge_roles: list[str] | None = None
    # v0.18.0 C3: per-role weighting for specialist judges. ``None``
    # (default) gives every judge equal weight. When set, each role's
    # vote is weighted by the corresponding float (e.g.
    # ``{"test_engineer": 2.0}`` doubles the test engineer's Borda
    # contribution). Roles missing from the dict default to weight 1.0.
    judge_role_weights: dict[str, float] | None = None
    # v0.22.0 Phase 4 (anti-bloat): optional model override for the
    # ``minimality_judge`` specialist role. ``None`` (default) uses the
    # cohort's resolved judge model — so the specialist runs on whatever
    # the operator configured for ``cfg.agents["judge"].model`` (or the
    # platform-default fallback). Setting this to a specific alias
    # (e.g. ``"sonnet"``) forces the minimality judge onto a single
    # model regardless of cohort defaults — useful when the operator
    # wants the minimality judge to use a smaller/cheaper model than
    # the correctness judges.
    minimality_judge_model: str | None = None
    # v0.22.0 Phase 4 (anti-bloat): absolute token-count threshold for
    # the oversize-candidate demotion check. When > 0, ANY candidate
    # whose markdown body exceeds this many estimated tokens is demoted
    # to the next-best Borda winner — a generalization of the legacy
    # AB-only ``max_plan_lines_growth_ratio`` check (which is preserved
    # alongside this field; both fire independently).
    # Default 4000 ≈ 4× the published 800-1000-character verbosity-bias
    # inflection point reported in Li et al. 2025 ("Mitigating Verbosity
    # Bias in LLM-as-Judge", arxiv 2506.09443, Fig. 5). Set to 0 to
    # disable the absolute check (legacy ratio-based demotion still runs).
    oversized_demotion_token_threshold: int = 4000
    # v0.18.0 B2: per-family plateau detection toggle. When True, the
    # multi-branch dispatcher checks
    # :class:`orchestrator.plateau_detector.PlateauDetector` for each
    # branch's family before fan-out and forces a ``distant-scout`` lane
    # change on the first plateaued branch. Default False — opt-in
    # because the detector requires non-trivial knowledge state.
    plateau_detection_enabled: bool = False
    plateau_window: int = 4
    cross_family_plateau_enabled: bool = False
    cross_family_plateau_window: int = 10

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


class CodeSizeThresholds(BaseModel):
    """v0.22.0 Phase 1: Fontana 2015 anchors for the code-size gate.

    All thresholds are *inclusive ceilings* — a value > the threshold trips
    the warn. Defaults are the v1 Fontana 2015 anchors documented in the
    plan (cyclomatic > 20 = warn-eligible; LOC per function > 100 =
    block-eligible). Operators may relax them per-repo via
    ``cfg.qa_gates.code_size_thresholds`` in ``.autodev/config.json``.
    """

    model_config = ConfigDict(extra="forbid")

    cyclomatic_max: int = 20  # Fontana 2015 (highly complex)
    loc_per_function: int = 100  # Fontana 2015 (block-eligible)
    dead_symbols: int = 0
    commented_out_blocks: int = 0
    duplicate_clusters: int = 0


class QAGatesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    syntax_check: bool = True
    lint: bool = True
    build_check: bool = True
    test_runner: bool = True
    secretscan: bool = True
    # v0.19.0: per-repo secretscan baseline. When True, ``run_secretscan`` is
    # diff-filtered against ``.autodev/secretscan-baseline.json`` so only
    # net-new findings vs. the baseline trip the gate. Refresh the baseline
    # via ``autodev secretscan baseline``.
    secretscan_baseline_enabled: bool = False
    # v0.22.1 A2: minimal safety valve for huge repos. On 358K-file Unity,
    # secretscan flagged 27K-50K false positives (Unity asset GUIDs clear
    # the 4.5 entropy default). When True (default), the gate auto-skips
    # with severity=warn on repos where ``runtime.repo_probe`` reports
    # ``is_huge``. Override per-repo with ``secretscan_force_run_on_huge_repo=True``.
    # Full FP redesign (entropy bump, ignore_paths, diff-mode default) is
    # deferred to v0.23.0 C2.
    secretscan_auto_skip_huge_repo: bool = True
    secretscan_force_run_on_huge_repo: bool = False
    # v0.23.0 C2: gitignore-style globs for files secretscan should skip
    # entirely. Composes with ``.autodev/secretscan-allow`` (which uses
    # the same syntax). Ships empty so existing behavior is preserved;
    # operators on huge repos with test-fixture density add e.g.
    # ``["**/Tests/**", "**/Fixtures/**", "**/TestData/**", "**/*.unity.meta"]``.
    secretscan_ignore_paths: list[str] = Field(default_factory=list)
    # v0.23.0 C2: per-call override for the global entropy threshold
    # (legacy default 4.5). Bumping to 4.8 suppresses Unity asset GUID
    # false positives (32-char hex with entropy ~4.3-4.7). ``None`` keeps
    # the module default.
    secretscan_entropy_threshold: float | None = None
    # v0.23.0 C2: minimum length for entropy-based string detection
    # (legacy default 20). Bumping to 32 filters short hex strings
    # ubiquitous in test fixtures while preserving real-world key
    # detection (most key formats are 32+ chars). ``None`` keeps default.
    secretscan_min_entropy_length: int | None = None
    # v0.19.0: per-extension entropy override. ``None`` means "use module
    # default curve" (see ``qa.secretscan._DEFAULT_PER_EXTENSION_ENTROPY``).
    secretscan_per_extension_thresholds: dict[str, float] | None = None
    # These two fields are NOT dispatched by _run_qa_gates. They are consumed
    # exclusively by agent prompts (e.g. architect.md) to drive security-tier
    # routing decisions at planning time. Dispatching them as actual gates is
    # planned in ADR-008 (see line 104 of this file).
    sast_scan: bool = False
    mutation_test: bool = False
    # v0.19.0: dispatch toggle for the mutation-test gate. Distinct from
    # ``mutation_test`` (planning-time advisory): when True, ``mutation_test``
    # is run as an actual gate via ``qa.mutation_test.run_mutation_test``.
    mutation_test_enabled: bool = False
    mutation_test_threshold: float = 0.7
    # v0.22.0 Phase 1: opt-in code-size (anti-bloat) gate. Off by default
    # for v1 (warn-only — see ``qa.code_size``). Flip to True per-repo to
    # surface deterministic syntactic-bloat findings on the diff.
    code_size: bool = False
    code_size_baseline_enabled: bool = False
    code_size_thresholds: CodeSizeThresholds | None = None
    # v0.22.1 A1: per-file timeout for regex-based QA gate scanners.
    # Triggered for the ``hallucination_guard`` C++ scanner after the
    # 2026-05-09 Unity stall (catastrophic regex backtracking on a
    # 358K-file repo). When a single-file scan exceeds this wall-clock
    # ceiling, the gate logs ``regex_timeout`` and skip-and-warns rather
    # than pinning the orchestrator. Tune lower on huge repos.
    regex_timeout_per_file_s: float = 10.0


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


class PRMConfig(BaseModel):
    """v0.20.0 A1: configuration for the trajectory PRM (Process Reward Model).

    The rule-based detectors in :mod:`orchestrator.prm` always run.
    ``strategy`` controls whether an additional LLM-based classifier
    augments the rule output:

    * ``"rules"`` (default) — rule-based only, byte-identical to v0.19.0.
    * ``"rules+ml"`` — rules-primary; LLM-secondary. Both run; the
      orchestrator merges results (rules win on dedup; ML pattern
      confidence ≥ ``ml_threshold``).

    ``ml_threshold`` is the LLM-confidence cutoff below which the
    classifier discards a pattern. 0.7 is the empirical default —
    higher cutoffs admit fewer patterns; lower admits more (but more
    false positives).
    """

    model_config = ConfigDict(extra="forbid")

    strategy: Literal["rules", "rules+ml"] = "rules"
    ml_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    # Minimum trajectory length before the LLM classifier is invoked.
    # Cold-start guard — the rule-based detectors already handle short
    # windows; running the LLM on 1–2 events wastes tokens.
    ml_min_events: int = Field(default=3, ge=1)


class PlateauDetectorConfig(BaseModel):
    """v0.20.0 A2: configuration for the plateau detector.

    ``strategy="rules"`` (default) preserves the v0.18.0 rule-based
    detection (no winner_promoted in trailing window). ``"regression"``
    swaps in a least-squares regression on
    ``cumulative winner_promoted_count`` vs event index — slope below
    ``plateau_slope_threshold`` flags a plateau.
    """

    model_config = ConfigDict(extra="forbid")

    strategy: Literal["rules", "regression"] = "rules"
    regression_window: int = Field(default=10, ge=3)
    plateau_slope_threshold: float = Field(default=0.1, ge=0.0)


class TaskOverridesConfig(BaseModel):
    """v0.20.0 D1: per-task overrides for the ``max_turns`` resolver.

    Currently scopes the per-bucket huge-repo multipliers introduced in
    v0.20.0 D1. ``None`` (default) uses the curve baked into
    :data:`runtime.repo_probe._HUGE_BUCKET_MULTIPLIERS` (simple 3.0×,
    medium 2.0×, complex 1.5×). Operator overrides are merged on a
    per-bucket basis: any bucket missing from the dict falls through to
    the default.

    Example::

        cfg.task_overrides.huge_repo_multipliers = {
            "simple":  4.0,   # navigate-heavy huge repo
            "complex": 1.2,   # already-generous bucket; modest bump
        }
    """

    model_config = ConfigDict(extra="forbid")

    huge_repo_multipliers: dict[str, float] | None = None


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


class DecayCurveConfig(BaseModel):
    """v0.20.0 B1: per-event-type confidence-decay curve.

    A decay curve maps a lesson's age (delta between now and its
    timestamp) to a multiplier applied to its confidence score during
    ranking. Two parameters:

    * :attr:`half_life_days` — age (in days) at which the curve hits
      ``floor + (1 - floor) / 2``. Larger values = slower decay.
    * :attr:`floor` — asymptote of the decay (the lowest factor an
      arbitrarily-old lesson can produce). Range ``[0.0, 1.0]``.

    The curve formula used by :func:`state.knowledge._recency_factor` is
    a smoothed linear blend toward the floor that matches the legacy
    behavior when ``half_life_days=15`` and ``floor=0.5`` — i.e. the
    legacy 30-day-window/0.5-floor curve hits 0.75 at 15 days and 0.5
    at 30 days. Per-event-type tuning lets ``winner_promoted`` lessons
    decay slower (still useful months later) while ``soft_blocker``
    lessons decay faster (early failures often inform near-term passes
    more than month-old ones).
    """

    model_config = ConfigDict(extra="forbid")

    half_life_days: float = Field(default=15.0, gt=0.0)
    floor: float = Field(default=0.5, ge=0.0, le=1.0)


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
    # v0.18.0 B1: lane-aware lesson injection toggle. When True (default),
    # :meth:`KnowledgeStore.inject_block` filters lessons by branch lane
    # when a ``lane=`` argument is supplied: only lessons whose metadata
    # lane matches OR have no lane tag (universal) are injected. When
    # False, lane filtering is disabled — equivalent to v0.17.0 behavior.
    # Default ON because the cost is negligible (one dict lookup per
    # entry) and the precision win on multi-branch runs is significant.
    lane_aware_injection_enabled: bool = True
    # v0.20.0 B1: per-event-type confidence-decay curves. ``None`` (default)
    # preserves byte-identical legacy behavior — every lesson uses the
    # 30-day linear decay (1.0 → 0.5 over 30 days). When set, each entry
    # is keyed by ``metadata["event_type"]`` and looked up here; entries
    # without an event_type or without a matching curve fall back to the
    # legacy curve. Example::
    #
    #     decay_curves = {
    #         "winner_promoted": DecayCurveConfig(half_life_days=30, floor=0.5),
    #         "soft_blocker":     DecayCurveConfig(half_life_days=7,  floor=0.4),
    #     }
    decay_curves: dict[str, DecayCurveConfig] | None = None
    # Phase 2 (anti-bloat): bootstrap hive-tier knowledge with curated
    # anti-pattern packs at orchestrator entry. ``seed_packs_enabled``
    # is the master switch. ``seed_packs`` lists pack basenames resolved
    # against the repo's ``seeds/`` directory (e.g. ``"anti_bloat_v1"``
    # -> ``seeds/anti_bloat_v1.jsonl``). Loading is idempotent via the
    # marker file ``.autodev/seed_packs.json`` and the existing
    # bigram-Jaccard dedup at :attr:`dedup_threshold`. Default ON because
    # an empty hive on a fresh project means reviewers/critics get no
    # anti-bloat guidance until enough swarm lessons accumulate to
    # promote.
    seed_packs_enabled: bool = True
    seed_packs: list[str] = Field(default_factory=lambda: ["anti_bloat_v1"])


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
    # v0.20.0 D1: per-bucket huge-repo multipliers for ``max_turns``
    # resolution. Default :class:`TaskOverridesConfig` carries
    # ``huge_repo_multipliers=None`` — the resolver uses the baked-in
    # curve (simple 3.0×, medium 2.0×, complex 1.5×).
    task_overrides: TaskOverridesConfig = Field(default_factory=TaskOverridesConfig)
    # v0.20.0 A1: PRM (trajectory pattern) detection strategy + threshold.
    # Default ``strategy="rules"`` preserves byte-identical v0.19.0 behavior.
    prm: PRMConfig = Field(default_factory=PRMConfig)
    # v0.20.0 A2: plateau-detector strategy. Default ``strategy="rules"``
    # preserves v0.18.0 rule-based detection (no-winner-in-window).
    plateau_detector: PlateauDetectorConfig = Field(
        default_factory=PlateauDetectorConfig
    )
    hive: HiveConfig
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    # v0.16.0 hallucination-guard top-level toggle. Default True — the
    # guard ships on by default so projects benefit immediately. Skip
    # patterns: dynamic imports, third-party packages not installed in
    # the scan environment, syntax errors. Set to False to silence the
    # gate entirely (e.g. for projects that mostly use untyped dynamic
    # imports and would generate too much noise).
    hallucination_guard: bool = True
    # v0.17.0 S4: bigram-Jaccard threshold for the multi-branch
    # repeated-hypothesis detector. The detector walks past 14 days of
    # ``discard`` events and tags branches whose hypothesis matches a
    # prior failure at or above this similarity. Advisory only (does
    # NOT block branch execution). Set to ``0`` to disable the check
    # entirely. Default ``0.6`` mirrors :attr:`KnowledgeConfig.dedup_threshold`.
    repeated_hypothesis_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    # v0.17.0 S2: opt-in web-search escalation step in the stuck-recovery
    # ladder. When True, the executor consults a web-search adapter at
    # ``pivot_count >= 2 AND search_count < 3`` (per-task cooldown) and
    # splices the top-3 results into the next critic_sounding_board
    # prompt as a ``WEB_CONTEXT:`` block. Default False (privacy-preserving
    # opt-in) — sites are queried only when explicitly enabled.
    web_search_enabled: bool = False
    # v0.17.0 S6: opt-in worktree sparse-checkout. When True, the
    # per-task worktree creation calls
    # ``git worktree add --no-checkout`` then narrows the working tree to
    # ``phase.edit_scope or plan.edit_scope`` via ``sparse-checkout
    # set``. Falls back to a full checkout (with a warning) if git is
    # older than 2.25. Default False — sparse-checkout speeds up huge
    # repos but breaks tasks that need files outside the declared scope.
    worktree_sparse_checkout_enabled: bool = False
    # v0.23.0 C1: huge-repo mode. ``"auto"`` keys off
    # ``runtime.repo_probe.RepoCapacity.is_huge`` (file_count > 20K OR
    # total_bytes > 5 GB) — when True, sparse-checkout becomes the
    # default for per-task worktrees regardless of
    # ``worktree_sparse_checkout_enabled``, and the worktree create
    # timeout extends to ``worktree_huge_create_timeout_s`` (the v0.22.1
    # A3 field). ``"on"`` forces huge-repo behavior; ``"off"`` disables
    # it even on huge repos (legacy escape hatch).
    worktree_huge_repo_mode: Literal["auto", "on", "off"] = "auto"
    # v0.23.0 C1: extended timeout for ``git worktree add`` when
    # huge_mode is on. Mirrors the WorktreeManager constructor param
    # so operators can override the orchestrator's wiring without
    # threading a new arg through every call site.
    worktree_huge_create_timeout_s: int = Field(
        default=600, ge=60, le=3600
    )
    # v0.23.0 C1: huge-repo pool sizing. Keep the warm pool small on
    # huge repos to reduce upfront cold-start time and disk pressure.
    worktree_huge_pool_size: int = Field(default=2, ge=0, le=8)
    # v0.21.0 A1: opt-in worktree warm-start pool. When True, the
    # orchestrator pre-creates ``resolve_parallelism(role_mix='execute')``
    # worktrees at execute-phase entry and recycles them via
    # ``git reset --hard <baseline> && git clean -fdx`` instead of paying
    # ``git worktree add`` cost on every task dispatch. Persistent dir:
    # ``.autodev/execute_worktrees_pool/``. Default False (opt-in)
    # because cold-start adds 2-5 s of upfront latency that's only
    # worthwhile on multi-task plans.
    worktree_pool_enabled: bool = False
    # v0.21.0 B1: opt-in cross-phase parallelism. When True, the execute
    # dispatcher allows tasks from phase N+1 to begin executing while
    # phase N's tail tasks finish, provided the new task's files don't
    # overlap any in-flight task's files AND its dependencies are
    # terminal. Phase-review still fires only after ALL tasks in the
    # phase reach terminal — its diff range uses
    # ``Phase.end_checkpoint_commit`` (captured at that moment) so phase
    # N+1's concurrent commits don't pollute phase N's review. Default
    # False (opt-in) because the priority-queue scheduler is novel and
    # operators may want to validate behavior on their plan before
    # opting in.
    cross_phase_parallelism_enabled: bool = False
    # v0.21.0 B2: opt-in speculative execution. When True, the execute
    # dispatcher may speculatively start ONE child task while its
    # parent is still in-flight (parent.retry_count==0 + child has a
    # SINGLE parent + file-disjoint with all in-flight). If the parent
    # later succeeds, the speculative work is valid. If the parent
    # fails, the speculative worktree is reset and the speculative task
    # re-queues as pending. Default False (opt-in) because rollback
    # complexity warrants cautious adoption.
    speculative_execution_enabled: bool = False

    # v0.25.0: file/symbol index for planner candidate lookup. The index
    # is a sqlite-FTS5 database at ``.autodev/index.db`` (path is
    # cwd-relative; override via ``index_path``) covering tracked files
    # and their top-level symbols (functions, classes, methods,
    # namespaces, structs). Built at ``autodev init`` and refreshed
    # incrementally on every ``autodev execute``/``plan``/``resume``;
    # queried by ``orchestrator.plan_phase`` to inject a CANDIDATE_FILES
    # block into the architect's envelope so it picks real paths in the
    # first place. Set ``index_enabled=False`` (or set the env var
    # ``AUTODEV_INDEX_DISABLED=1``) to opt out — the architect prompt's
    # "PREFER paths from this list" instruction degrades into a no-op
    # when the index is missing.
    index_enabled: bool = True
    # cwd-relative path to the sqlite index database. Operators rarely
    # change this; the default lands the file inside ``.autodev/`` so it
    # gets cleaned up by ``rm -rf .autodev/``.
    index_path: str = ".autodev/index.db"
    # ``None`` = auto-detect (Python via ``ast``, C++ via tree-sitter +
    # the existing ``qa.cpp_symbols`` extractor, TypeScript via
    # ``tree-sitter-typescript`` when installed else regex fallback).
    # Pass an explicit list to opt-in to a subset, e.g. ``["py"]`` for a
    # pure-Python project.
    index_languages: list[str] | None = None
    # When ``IndexBuilder.build_incremental`` would touch more files than
    # this threshold, it delegates to ``build_full`` instead — at some
    # point a "smart" incremental refresh costs more than just rebuilding
    # the index from scratch (sqlite WAL contention + per-file tree-sitter
    # parse overhead). 5000 is a conservative default; raise it for
    # high-churn repos where full rebuilds are expensive.
    index_full_rebuild_threshold_files: int = Field(
        default=5000, ge=100, le=100_000
    )
    # On huge repos (``RepoCapacity.is_huge``) the initial full build can
    # take minutes. When True (default), ``autodev init`` spawns the
    # builder in a background subprocess and returns immediately; the
    # ``.autodev/index.db.building`` marker file signals to the per-trigger
    # incremental hook that it should skip until the build completes.
    # Set False to force synchronous initial build even on huge repos.
    index_huge_repo_async_init: bool = True

    def require_all_roles(self) -> None:
        """Raise ValueError if any required role is missing from `agents`."""
        missing = [r for r in REQUIRED_AGENT_ROLES if r not in self.agents]
        if missing:
            raise ValueError(f"missing required agent roles: {missing}")
