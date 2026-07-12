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


# v0.42.0 (C1): specialist roles introduced after the original 14 required
# roles. They are dispatched via a *self-contained* path (each phase reads
# ``cfg.agents[role]`` for model/max-turns and invokes the role directly,
# like ``framing._invoke_framing_role``) — NOT through ``build_registry``,
# which only holds :data:`REQUIRED_AGENT_ROLES`. Because they are not
# required, ``require_all_roles()`` never validated them, so a pre-v0.41
# ``config.json`` (written before these roles existed) lacks the
# ``cfg.agents[role]`` entry → ``KeyError`` at dispatch → silent fail-safe
# degrade (the Run-4 DEAD-ON-ARRIVAL bug for intake/diagnosis). The fix is a
# load-time backfill (see ``config.loader.load_config``) that adds a default
# :class:`AgentConfig` for any specialist role missing from an on-disk config,
# idempotently (operator customizations are never overwritten).
#
# Keep this in sync with the specialist keys in
# ``config.defaults._AGENT_MODEL_DEFAULTS`` / ``_AGENT_MAX_TURNS``.
SPECIALIST_ROLES: tuple[str, ...] = (
    "framing",
    "altitude_judge",
    "intake_enricher",
    "intake_clarifier",
    "diagnostician",
    # v0.42.0 (ADR-0047): the Universal Blocker Resolver role. Registered as a
    # specialist (self-contained dispatch, forced structured output) so it can
    # never repeat the DOA bug it was built to eliminate.
    "resolver",
)


# v0.38.0 HK1: one-shot warning ledger for the ``"coder"`` → ``"developer"``
# kind-label migration shim. Pydantic's ``model_validator(mode="after")``
# can fire many times per process (one per config load + per model copy),
# so the warning is gated on this module-level set to avoid log spam in
# long-running fleets and test runs. Scheduled for removal in v0.39.0
# alongside the shim itself.
_warned_kind_labels: set[str] = set()


def _emit_kind_deprecation_warning() -> None:
    """Emit a one-shot warning for the legacy ``"coder"`` kind label.

    Re-entrant by design: the second + Nth call within the same process
    are no-ops so resume / replay / repeated config loads do not flood
    the log. The structured warning fires through the autologging
    pipeline so operators see it in both the stdlib ``warnings`` stream
    and the structured-log forensics trail.
    """
    if "coder" in _warned_kind_labels:
        return
    _warned_kind_labels.add("coder")
    import warnings as _warnings

    from autologging import get_logger as _get_logger

    _warnings.warn(
        "recent_evidence_include_kinds: 'coder' is deprecated in "
        "v0.38.0 and will be removed in v0.39.0. Treated as "
        "'developer' (the on-disk kind label). Update "
        ".autodev/config.json to use 'developer' directly.",
        DeprecationWarning,
        stacklevel=3,
    )
    _get_logger(__name__).warning(
        "config.deprecated_kind_label",
        deprecated="coder",
        replacement="developer",
        scheduled_removal="v0.39.0",
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
    # v0.36.0 D3: model used for architect retries when the failure
    # class is structural (path validation, plan parse) rather than
    # reasoning. Bumps from opus → sonnet on retry 2+ for the
    # ``missing_on_disk`` / ``new_md_deliverable`` classes — sonnet
    # corrects path lists as well as opus and saves ~$3-5/attempt.
    # Currently honoured only by the architect role (other roles ignore
    # it). Set to None / empty to disable the routing.
    structural_retry_model: str = "sonnet"


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
    # v0.25.3: per-tournament auto-disable list. Each runner consults the
    # field on its own phase config. ``None`` (the default at the schema
    # level) means "inherit from :attr:`TournamentsConfig.auto_disable_for_models`
    # if that's non-empty, otherwise fall back to a per-phase built-in
    # default applied in :meth:`TournamentsConfig._resolve_auto_disable`":
    #
    #   * plan defaults to ``[]`` (always run, even on Opus — plan errors
    #     compound through every downstream task and the cost is justified).
    #   * impl defaults to ``["opus"]`` (cost guard: one tournament per task).
    #   * phase_review defaults to ``["opus"]`` (cost guard: per-phase fan-out).
    #
    # An explicit list at this level always wins over the top-level
    # fallback — no silent inheritance.
    auto_disable_for_models: list[str] | None = None
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


class FramingPhaseConfig(BaseModel):
    """Framing/altitude phase config (ADR-0044).

    Mirrors :class:`TournamentPhaseConfig`'s ``extra="forbid"`` strictness.
    ``enabled`` has no inline default — it is set by :func:`_default_framing_cfg`
    so the ``default_factory`` on :attr:`AutodevConfig.framing` keeps legacy
    on-disk configs (which omit the field) valid.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    design_smell_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    num_approaches: int = Field(default=3, ge=2, le=3)
    require_structural_signal: bool = True
    altitude_judge_panel_size: int = Field(default=3, ge=1, le=5)
    classifier_model: str | None = None
    altitude_judge_model: str | None = None


def _default_framing_cfg() -> "FramingPhaseConfig":
    """Default-on framing config used when an existing ``config.json`` omits the
    new field (ADR-0044). On by default; the conservative classifier + fail-safe
    degrade to ``local_defect`` are the offset."""
    return FramingPhaseConfig(
        enabled=True,
        design_smell_threshold=0.7,
        num_approaches=3,
        require_structural_signal=True,
        altitude_judge_panel_size=3,
        classifier_model=None,
        altitude_judge_model=None,
    )


class IntakePhaseConfig(BaseModel):
    """Behavioral config for the intake & clarification phase (ADR-0045).

    Mirrors :class:`FramingPhaseConfig`'s ``extra="forbid"`` strictness.
    ``enabled`` has no inline default — it is set by :func:`_default_intake_cfg`
    so the ``default_factory`` on :attr:`AutodevConfig.intake` keeps legacy
    on-disk configs (which omit the field) valid. On by default, but a no-op
    for well-formed specs (the completeness gate is a cheap deterministic scan).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    # Host question-UI cap (the §3.5 wire contract renders at most this many).
    max_questions: int = Field(default=4, ge=1, le=4)
    sources: list[str] = Field(default_factory=lambda: ["repo", "github", "jira"])
    # Benchmark contamination guard — never pull content matching these globs
    # (e.g. the solution PR branch). Mirrors the framing exclude pattern.
    exclude_globs: list[str] = Field(default_factory=list)
    # Headless policy when no operator answers the clarifying questions.
    on_unanswered: Literal["assume_defaults", "block", "fail"] = "assume_defaults"
    # Repo gather rides the existing explorer evidence (no second pass).
    reuse_explorer_evidence: bool = True
    enricher_model: str | None = None
    clarifier_model: str | None = None


def _default_intake_cfg() -> "IntakePhaseConfig":
    """Default-on intake config used when an existing ``config.json`` omits the
    new field (ADR-0045). On by default; the no-op fast path on well-formed
    specs (+0 LLM calls) and the headless ``assume_defaults`` policy are the
    offset (cron/CI never deadlock)."""
    return IntakePhaseConfig(
        enabled=True,
        max_questions=4,
        sources=["repo", "github", "jira"],
        exclude_globs=[],
        on_unanswered="assume_defaults",
        reuse_explorer_evidence=True,
        enricher_model=None,
        clarifier_model=None,
    )


class DiagnosisPhaseConfig(BaseModel):
    """Behavioral config for the diagnosis phase (ADR-0046).

    Mirrors :class:`FramingPhaseConfig`'s ``extra="forbid"`` strictness.
    ``enabled`` has no inline default — it is set by :func:`_default_diagnosis_cfg`
    so the ``default_factory`` on :attr:`AutodevConfig.diagnosis` keeps legacy
    on-disk configs (which omit the field) valid. On by default, but bug-gated
    (``bug_only=True``) so feature work skips the phase entirely (+0 cost).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    bug_only: bool = True
    max_hypotheses: int = Field(default=5, ge=3, le=5)
    require_loop_to_plan: bool = True
    # Sandbox fallback when only a *live* loop reproduces (§5.2): build a
    # synthetic/replay loop + deliver a live-repro artifact, or block.
    on_no_live_loop: Literal["synthetic_plus_artifact", "block"] = (
        "synthetic_plus_artifact"
    )
    diagnostician_model: str | None = None


def _default_diagnosis_cfg() -> "DiagnosisPhaseConfig":
    """Default-on diagnosis config used when an existing ``config.json`` omits
    the new field (ADR-0046). On by default but bug-gated; the synthetic-loop +
    delivered-artifact fallback keeps reproduce-first possible headlessly
    instead of deadlocking on network/credential-bound bugs."""
    return DiagnosisPhaseConfig(
        enabled=True,
        bug_only=True,
        max_hypotheses=5,
        require_loop_to_plan=True,
        on_no_live_loop="synthetic_plus_artifact",
        diagnostician_model=None,
    )


class ResolverConfig(BaseModel):
    """Universal Blocker Resolver config (ADR-0047).

    Mirrors :class:`FramingPhaseConfig`'s ``extra="forbid"`` strictness and
    ``default_factory`` wiring so a legacy ``config.json`` lacking the field
    still validates. The resolver is the orchestrator-level catch-all that
    fires when a downstream agent/phase hits a *terminal* blocker (or an
    *unrecognized* failure): it reasons about the blocker and chooses a bounded
    recovery action to re-enable the workflow instead of dead-ending at
    ``blocked: user_decision_required`` or silently degrading.

    Two-tier design: the existing deterministic recovery ladder is the cheap
    fast path; this resolver is the catch-all above it. With
    ``fast_path_only_on_known=True`` (default) the resolver only fires at
    terminal rungs or on failure classes the deterministic ladder does not
    recognise, so the common-case cost stays ≈ 0.

    Kill-switch: ``AUTODEV_RESOLVER_DISABLED=1`` forces the resolver off
    regardless of ``enabled`` (every call site falls through to its prior
    block/degrade behaviour — fail-safe).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    # Per-blocker resolution budget (ledger-tracked). Once a single blocker has
    # consumed this many resolver cycles without recovery, the resolver stops
    # recursing and falls through to a bounded ``ask_human`` (loop-safety B5).
    max_cycles_per_blocker: int = Field(default=3, ge=1, le=10)
    # F-2 (field-finding): PHASE-scoped ceiling on consecutive same-failure-class
    # corrective regeneration. ``max_cycles_per_blocker`` is keyed per (task_id,
    # failure_class) and so never bounds the CROSS-corrective-task loop (each
    # freshly-minted corrective has a NEW id => a fresh per-task counter). This
    # sibling counter is keyed on the PHASE + failure_class, so corrective-task-id
    # churn cannot reset it. Once this many consecutive same-class correctives are
    # minted WITHOUT forward progress, the resolver STOPS minting and declines —
    # emitting a LOUD, attributable ``corrective_nonconvergent_ceiling`` ledger op
    # and letting the originating ``block_task`` commit the single terminal block
    # (instead of churning to the 40-min execute wall). Mirrors
    # ``max_cycles_per_blocker``'s default (3). Reset on forward progress (a task
    # in the phase completing) or when a DIFFERENT failure_class occurs (a new
    # distinct problem is not the same loop, since the counter is per-class).
    max_corrective_cycles_per_phase: int = Field(default=3, ge=1, le=10)
    # When True, the resolver only engages at terminal recovery rungs or on
    # failure classes the deterministic ladder does not handle (keeps cost ≈ 0
    # for the common, already-recoverable case). When False, the resolver is
    # consulted on every routed blocker (more LLM calls; useful for evaluation).
    fast_path_only_on_known: bool = True
    model: str | None = None
    # WS5 — ask_human dead-end policy. Every deterministic ladder terminates at
    # ``ask_human`` when exhausted, but an unattended run has no human-decision
    # channel, so the historical behaviour silently blocks the task. This field
    # is a GENUINE behaviour change with real blast radius, so it ships opt-in
    # (default ``"block"`` == today's behaviour, byte-for-byte):
    #   * ``"block"``               — decline ask_human; the caller does its
    #     legacy block/degrade (UNCHANGED — the regression pin).
    #   * ``"best_effort_commit"``  — when the ladder would resolve to
    #     ``ask_human``, attempt to apply whatever diff currently exists in the
    #     task's worktree; if non-empty AND it applies, mark the task complete
    #     (stamped ``needs_human_review`` + a distinct ledger op so a benchmark
    #     scorer treats it as its OWN terminal category, not "solved"). Nothing
    #     to commit / an apply that fails → falls through to the legacy block.
    #   * ``"fail"``                — raise ``AskHumanDeadEndError`` loudly at the
    #     point the ladder resolves to ``ask_human`` (a benchmark harness that
    #     prefers a hard failure to a silent block).
    on_ask_human: Literal["block", "best_effort_commit", "fail"] = "block"


def _default_resolver_cfg() -> "ResolverConfig":
    """Default-on resolver config used when an existing ``config.json`` omits
    the new field (ADR-0047). On by default; ``fast_path_only_on_known=True``
    plus the per-blocker cycle budget and the global circuit-breaker keep the
    common-case cost ≈ 0 and make runaway loops impossible."""
    return ResolverConfig(
        enabled=True,
        max_cycles_per_blocker=3,
        max_corrective_cycles_per_phase=3,
        fast_path_only_on_known=True,
        model=None,
        on_ask_human="block",
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
    # v0.25.3: deprecated top-level fallback. Existing on-disk configs
    # (v0.25.2 and earlier) wrote ``["opus"]`` here to disable all three
    # tournaments on Opus. v0.25.3 moves the policy into each
    # :class:`TournamentPhaseConfig` so plan tournaments can run on Opus
    # while impl / phase_review remain cost-guarded. The default flips
    # to ``[]`` so fresh installs hit the new per-tournament defaults;
    # legacy configs that still pin ``["opus"]`` here inherit it down
    # into every per-tournament slot whose own value is ``None``.
    auto_disable_for_models: list[str] = Field(default_factory=list)

    # v0.32.0 Phase 2: opt-in autoreason-style A/B/AB review pipeline.
    # When ``review_tournament_enabled=True``, the execute-phase reviewer
    # step swaps the legacy single-shot ``delegate(..., "reviewer", ...)``
    # call for an A/B/AB tournament routed through
    # :mod:`orchestrator.review_tournament_runner`. Default ``False``
    # because v0.32.0 ships the feature opt-in for one cycle (real-world
    # telemetry needed before flipping the default in v0.33.0). The
    # remaining knobs are intentionally minimal — most operators won't
    # touch them; the cohort + convergence defaults match the published
    # autoreason technique.
    review_tournament_enabled: bool = False
    review_num_judges: int = 3
    review_convergence_k: int = 2
    review_max_rounds: int = 5
    review_judge_roles: list[str] | None = None

    @model_validator(mode="after")
    def _resolve_auto_disable(self) -> "TournamentsConfig":
        """v0.25.3: resolve each per-tournament ``auto_disable_for_models``.

        AutoDev's goal is to improve the quality and consistency of
        AI-generated code, regardless of model cost. Tournaments must
        never be skipped by default. The auto-disable mechanism is
        retained as an explicit operator override (for cost-controlled
        development environments) but its built-in default is empty for
        every tournament type.

        Precedence (per phase config):

        1. Explicit value at the per-tournament level (any non-``None``)
           wins, even when the top-level list disagrees.
        2. Otherwise, if the deprecated top-level
           ``auto_disable_for_models`` is non-empty, inherit it
           (back-compat with legacy on-disk configs from v0.25.2 and
           earlier).
        3. Otherwise, the per-phase built-in default is ``[]`` —
           tournaments run on every model, including Opus.
        """
        for phase_name in ("plan", "impl", "phase_review"):
            phase_cfg = getattr(self, phase_name)
            if phase_cfg.auto_disable_for_models is not None:
                continue  # explicit per-tournament value wins
            if self.auto_disable_for_models:
                phase_cfg.auto_disable_for_models = list(
                    self.auto_disable_for_models
                )
            else:
                phase_cfg.auto_disable_for_models = []
        return self


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
    # v0.26.1 patch B: operator-extensible vendor-tree skip list for the
    # hallucination_guard whole-tree walk. UNIONED with the built-in
    # default (``External``, ``Tools``, ``vendor``, ``third_party``,
    # ``third-party``, plus the build-artifact set). Use for project-
    # specific vendor directories not covered by the defaults.
    hallucination_guard_skip_dirs: list[str] = Field(default_factory=list)
    # v0.34.0 B1: in sparse-checkout worktrees the hallucination guard
    # cannot see the full include / import chain, so unresolved symbols
    # are downgraded to non-blocking ``unresolved_symbol`` findings. Flip
    # to False to preserve the legacy "block on any hallucination" path
    # even when the worktree is sparse (operators on small projects or
    # with full local resolution may prefer the strict default).
    hallucination_guard_sparse_downgrade: bool = True
    # v0.40.1: configurable test-gate wall-clock timeout (seconds). The legacy
    # hardcoded 60s could not finish large pytest suites; default 600s gives
    # real suites headroom. Threaded into ``run_tests(timeout_s=...)``.
    test_timeout_s: float = 600.0
    # v0.40.1: configurable lint-gate wall-clock timeout (seconds). Lint is
    # fast, but scoping to changed files on a huge repo can still spin up the
    # target's env (uv run / .venv); 120s is a safe ceiling. Threaded into
    # ``run_lint(timeout_s=...)``.
    lint_timeout_s: float = 120.0
    # WS2-11: configurable build/typecheck-gate wall-clock timeout (seconds).
    # The legacy hardcoded 60s (``build_check._DEFAULT_TIMEOUT_S``) could not
    # finish a COLD cargo/Go build (dependency fetch + first compile), so the
    # gate timed out and false-blocked. 120s is a safe floor; bump higher for
    # large native projects. Threaded into ``run_build_check(timeout_s=...)``.
    build_check_timeout_s: float = 120.0


class GuardrailsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Upper bound on agent round-trips per task (enforced in pre_invocation).
    max_invocations_per_task: int = 60
    # Upper bound on cumulative tool calls per task. Requires stream-json
    # parsing (Phase 3 functionality) to be fully enforced; currently
    # tool_calls are populated only when stream-json is used.
    max_tool_calls_per_task: int = 60
    # v0.26.1 patch F: bumped from 900s → 2400s. The 900s default
    # predated the v0.8.0 per-complexity timeout escalation
    # (``tournament.task_overrides.TASK_TIMEOUT_S_DEFAULTS["complex"] =
    # 1800``). With 1800s available for a single complex developer call
    # plus reviewer headroom (~600s), 2400s is the new floor below
    # which legitimate runs trip on the guardrail rather than on the
    # subprocess timeout. Operators with explicit values are unaffected.
    max_duration_s_per_task: int = 2400
    # v0.42.0 (C5): a tighter duration cap selected by the enforcer for
    # *corrective test-run / test-repair* tasks specifically. These tasks run a
    # test suite and apply a small repair; they have a different (usually
    # shorter) duration profile than a full developer task, and the v0.41
    # field's 2400s default let a wedged corrective test-repair burn the whole
    # window. ``None`` (default) means "fall back to ``max_duration_s_per_task``"
    # — byte-identical to v0.41 behaviour until an operator sets it (or the
    # resolver's ``relax_constraint``/``split_task`` actions reach it).
    max_duration_s_per_test_repair_task: int | None = None
    max_diff_bytes: int = 5_242_880
    cost_budget_usd_per_plan: float | None = None
    # F-7 (field-finding): cumulative WALL-CLOCK budget (seconds) for the
    # plan-tournament pass loop, analog of ``max_corrective_cycles_per_phase``
    # (F-2) but bounding wall-clock instead of corrective-cycle count. The
    # plan tournament runs through ``AdapterLLMClient`` which BYPASSES the
    # guardrail enforcer's pre/post_invocation — so there is NO cumulative
    # deadline anywhere in the plan-tournament loop. A slow OR wedged plan
    # phase therefore has no fail-loud bound; it runs to
    # ``max_rounds × judges × branches`` or until an EXTERNAL SIGKILL,
    # surfacing as an opaque "timed out after 2400s" with no autodev-emitted
    # reason. When set (> 0), ``tournament.core.Tournament.run`` checks
    # cumulative elapsed BETWEEN passes (cheap; never mid-call) and, on
    # breach, STOPS LOUD with the best on-disk incumbent (the existing
    # plan-phase salvage path) while emitting the greppable, attributable
    # ``plan_phase_wall_budget_exceeded`` ledger op. ``None`` (default) =
    # OFF = byte-identical legacy behavior (no deadline). Set this BELOW an
    # external / benchmark per-command timeout (e.g. the 2400s
    # ``max_duration_s_per_task`` subprocess wall) so the plan phase fails
    # LOUD with a reason BEFORE being killed.
    plan_phase_wall_budget_s: float | None = None
    # Task 1 (wall-budget fix, sibling of F-7): cumulative WALL-CLOCK budget
    # (seconds) for the impl-tournament pass loop. ``run_impl_tournament``
    # (see ``orchestrator.impl_tournament_runner``) calls
    # ``orch.adapter.execute()`` DIRECTLY via ``_CoderRunner`` for the
    # developer / test_engineer round-trips, bypassing ``delegate()``
    # entirely — so ``GuardrailEnforcer.pre_invocation``/``post_invocation``
    # (and therefore ``max_duration_s_per_task``) never see these calls
    # either. A slow OR wedged impl tournament therefore has no cumulative
    # fail-loud bound of its own; it runs to ``max_rounds`` (default 3, each
    # pass ~10 serialized agent calls) or until an EXTERNAL SIGKILL,
    # surfacing as an opaque "timed out after Ns" with no autodev-emitted
    # reason — the same failure shape F-7 fixed for the plan tournament.
    # When set (> 0), the SAME engine ``plan_phase_wall_budget_s`` uses
    # (``tournament.core.Tournament.run``) checks cumulative elapsed
    # BETWEEN passes (cheap; never mid-call) and, on breach, STOPS LOUD
    # with the best on-disk incumbent while emitting the greppable,
    # attributable ``impl_phase_wall_budget_exceeded`` ledger op. ``None``
    # (default) = OFF = byte-identical legacy behavior (no deadline). Note
    # this bounds a SINGLE impl tournament's own pass loop only — it does
    # NOT bound an entire ``autodev execute`` invocation across many
    # tasks/tournaments (a separate, larger fix, ``execute_phase_wall_budget_s``,
    # addresses that DAG-wide ceiling).
    impl_phase_wall_budget_s: float | None = None
    # Task 2 (wall-budget fix, DAG-wide sibling of the two above): cumulative
    # WALL-CLOCK budget (seconds) spanning the ENTIRE execute-phase DAG within
    # ONE ``autodev execute`` / ``autodev resume`` invocation — across however
    # many tasks, retries, and tournaments run serially before the command
    # returns. This is genuinely SEPARATE from both:
    #   * ``max_duration_s_per_task`` bounds ONE task's agent round-trips via
    #     ``delegate()``'s ``GuardrailEnforcer.pre/post_invocation`` hooks —
    #     but the impl tournament calls ``orch.adapter.execute()`` DIRECTLY,
    #     bypassing ``delegate()`` entirely, so ``max_duration_s_per_task``
    #     structurally cannot see impl-tournament time; and it only ever
    #     bounds a single task, never the SUM.
    #   * ``impl_phase_wall_budget_s`` bounds ONE impl tournament's own pass
    #     loop only.
    # Nothing bounds the CUMULATIVE time across the many tasks/retries/
    # tournaments a single ``execute`` DAG runs serially — the exact shape of
    # the SWE-bench-Lite pilot that timed out at an opaque external 1800s
    # SIGKILL (4 impl tournaments across 4 tasks, serial under
    # ``max_parallel_subprocesses=1``). Tune this against an EXTERNAL
    # per-command timeout (a CI kill-after, a benchmark harness's subprocess
    # timeout) — NOT against ``max_duration_s_per_task``; the two numbers are
    # independently meaningful (one is per-task, this is whole-command). The
    # enforcer (``GuardrailEnforcer.start_execute_phase`` /
    # ``execute_phase_wall_budget_exceeded`` / ``check_execute_phase_wall_budget``)
    # checks cumulative elapsed CHEAPLY between tasks/retries/rounds (never
    # mid-call). On breach it raises
    # ``errors.ExecutePhaseWallBudgetExceededError`` and emits the greppable,
    # attributable ``execute_phase_wall_budget_exceeded`` ledger op. The task
    # in flight at breach time is left EXACTLY as-is (NOT stamped blocked /
    # quarantined) — the existing orphan-reap sweep
    # (``PlanManager.reap_orphans()``, which already runs at the top of every
    # ``run_execute_phase()`` call) reverts it to ``pending`` so a normal
    # ``autodev resume`` picks it back up; no new salvage machinery required.
    # ``None`` (default) = OFF = byte-identical legacy behavior (no deadline).
    # Set this BELOW an external / benchmark per-command timeout so the whole
    # execute phase fails LOUD with a reason BEFORE being killed.
    execute_phase_wall_budget_s: float | None = None


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

    Scopes both the per-bucket huge-repo multipliers (v0.20.0 D1) and,
    as of v0.36.0 E1, role-keyed huge-repo multipliers applied at
    dispatch time. The complexity-keyed lookup remains the canonical
    code path; the role-keyed dict feeds the
    ``huge_repo_multiplier_applied`` telemetry op AND surfaces in the
    config for operators who tune per-role budgets directly.

    v0.36.0 E1: default populated with role-shaped keys so operators
    don't have to opt-in for the v0.32 fixture (huge-repo explorer
    needs 3×, architect 2×, …). Any key absent from the dict falls
    through to the default curve baked into
    :data:`runtime.repo_probe._HUGE_BUCKET_MULTIPLIERS`.

    v0.36.0 E2: ``retry_budget_multiplier`` doubles the resolved budget
    on retry attempt ≥ 2 (capped at ``retry_budget_cap_turns``) so a
    task that legitimately needs more runway on retry gets it instead
    of burning another retry slot at the same budget.
    """

    model_config = ConfigDict(extra="forbid")

    # v0.36.0 E1 / v0.37.0 H5: populated role-keyed dict (was ``None``
    # through v0.35). Operators may still override with their own role
    # / complexity keys; missing keys fall through to the baked-in
    # default curve in :mod:`runtime.repo_probe`.
    #
    # v0.37.0 H5 added the H1/H2/H3 knob keys
    # (``max_corrective_tasks_per_phase``, ``test_diag_breaker_window_s``,
    # ``recent_evidence_max_chars_per_kind``, ``circuit_breaker_threshold``,
    # ``test_diag_breaker_threshold``, ``max_duration_s_per_task``,
    # ``max_diff_bytes``) so that the new H1–H3 caps auto-scale on huge
    # repos without operator config tuning. The
    # :func:`orchestrator.huge_repo_overrides.resolve_huge_repo_value`
    # resolver consults this dict and emits a
    # ``huge_repo_multiplier_applied`` telemetry op per scaled key.
    huge_repo_multipliers: dict[str, float] = Field(
        default_factory=lambda: {
            # Role-keyed (v0.36.0 E1).
            "explorer": 3.0,
            "architect": 2.0,
            # v0.38.0 HK1: ``"coder"`` is preserved as a back-compat
            # alias for ``"developer"`` in the role-keyed dict —
            # removing it would silently de-scale operator configs that
            # pinned the legacy role name. Scheduled for removal in
            # v0.39.0 alongside the recent_evidence_include_kinds shim.
            "coder": 2.0,
            # WS-2a (slice4 forensic, 2026-07-12): the developer floor was
            # raised 10 → 15 in ``config.defaults``. This 2.0× role key is
            # consumed for the huge-repo ``huge_repo_multiplier_applied``
            # telemetry op on the *task* dispatch path; the developer's actual
            # per-task huge-repo budget scaling flows through the
            # COMPLEXITY-keyed curve in ``tournament/task_overrides.py``
            # (``_HUGE_BUCKET_MULTIPLIERS``), not this role key.
            "developer": 2.0,
            # v0.39.0 C1: bumped 1.5 → 2.5 so non-task reviewer turns scale
            # enough on huge repos (base 5 × 2.5 ≈ 13 ≥ the empirically-
            # needed 12). Small-repo reviewer stays at the base 5.
            "reviewer": 2.5,
            "domain_expert": 1.5,
            # WS-2a (slice4 forensic, 2026-07-12): bumped 1.5 → 2.0. The
            # test_engineer floor was raised 8 → 12 in ``config.defaults``;
            # this role key genuinely scales that per-role budget on the
            # NON-task dispatch path (``execute_phase`` scales
            # ``spec_max_turns`` by the role multiplier when the repo is
            # huge). The heaviest write+run role must scale AT LEAST as much
            # as the lighter read+verdict roles (domain_expert 1.5), and to
            # parity with the developer key (2.0) — the prior 1.5 predated the
            # floor bump and under-scaled the role. Effective huge-repo budget:
            # 12 × 2.0 = 24.
            "test_engineer": 2.0,
            # WS-5 (slice4 forensic, 2026-07-12): the architect_b floor was
            # raised 5 → 8 in ``config.defaults`` for its new Read + Bash
            # reproduction workload. This role key gives it proportional
            # huge-repo headroom (8 × 2.0 = 16), parity with the exec-workload
            # roles (developer / test_engineer). It is LIVE + beneficial in the
            # consult ``delegate`` non-task-role path: the v0.39.0 C1 branch in
            # ``execute_phase.py`` scales ``spec_max_turns`` by a DIRECT
            # ``huge_repo_multipliers[role]`` dict lookup (NOT
            # ``resolve_huge_repo_value``), so a huge-repo consult raises
            # architect_b 8 → 16. It is dormant ONLY in the TOURNAMENT dispatch
            # path (``*_tournament_runner._build_role_overrides`` does not yet
            # scale tournament-role ``max_turns`` by this key — a follow-up
            # outside the WS-5 lane). The raised base floor (8) applies at all
            # sites regardless.
            "architect_b": 2.0,
            # v0.37.0 H5: knob-keyed. The :mod:`orchestrator.huge_repo_overrides`
            # resolver looks these up by knob name and applies the
            # multiplier to the operator's configured base value.
            "max_duration_s_per_task": 2.5,
            "max_diff_bytes": 3.0,
            "max_corrective_tasks_per_phase": 2.0,
            # v0.38.0 I3: plan-scope corrective ceiling mirrors the
            # per-phase auto-scale on huge repos. 2.0× = same elbow as
            # the per-phase knob so the two stay in sync.
            "max_corrective_tasks_per_plan": 2.0,
            "test_diag_breaker_window_s": 2.0,
            "recent_evidence_max_chars_per_kind": 1.5,
            "circuit_breaker_threshold": 2.0,
            "test_diag_breaker_threshold": 2.0,
            # v0.38.0 I4: budget-shaped knobs — wider window + bigger
            # cumulative budget so huge repos absorb their longer
            # per-run cadence before tripping the hard halt. The
            # per-event knobs (initial, multiplier, max-per-iter,
            # auto-reset-N) are NOT scaled because they're shaped per
            # event, not by total runtime.
            "test_diag_backoff_total_budget_s": 2.0,
            "test_diag_auto_reset_window_s": 2.0,
            # v0.39.0 huge-repo-native tier. The resolver looks these up by
            # name and applies the multiplier to the operator's base value.
            #   * probe_timeout_s (B2): 10s → 15s so the PONG preflight
            #     probe beats the ~7-10s cold start on huge repos. Base
            #     lives on ``cfg.adapters.probe_timeout_s`` (not a top-level
            #     attr), so it is scaled at the adapter, not via
            #     ``resolve_all_h5_knobs``.
            #   * parallelism_multiplier (B3): <1.0 by design — halves the
            #     auto-picked subprocess count (e.g. 12 → 6) to cut 429/529
            #     rate-limit pressure. Consumed by
            #     ``resolve_huge_repo_parallelism``, which clamps to a
            #     ceiling and treats operator pins as an escape hatch.
            #   * circuit_breaker_window_s (B4): widens the infra-failure
            #     window 60s → 120s so a slow huge-repo burst doesn't trip
            #     the breaker prematurely. ``circuit_breaker_threshold`` is
            #     already scaled elsewhere; this scales the RAW window at
            #     the breaker construction site.
            #   * max_turns_ceiling (A3): lifts the budget-escalation turns
            #     ceiling 250 → 375. Base lives on nested
            #     ``cfg.budget_escalation`` (not a top-level attr), scaled
            #     at the consume site, not via ``resolve_all_h5_knobs``.
            "probe_timeout_s": 1.5,
            "parallelism_multiplier": 0.5,
            "circuit_breaker_window_s": 2.0,
            "max_turns_ceiling": 1.5,
        }
    )
    # v0.36.0 E2: multiplier applied to the resolved ``max_turns`` on
    # retry attempts ≥ 2. Capped by ``retry_budget_cap_turns`` so a
    # cascade of retries can't push the per-task budget to infinity.
    retry_budget_multiplier: float = 2.0
    retry_budget_cap_turns: int = 200


class BudgetEscalationConfig(BaseModel):
    """v0.39.0 A3: ceilings for the per-task budget-escalation ladder.

    The escalation ladder in :mod:`orchestrator.budget_escalation` bumps a
    task's ``max_turns`` / ``timeout_s`` on successive retry attempts. These
    two ceilings cap that ladder so a cascade of retries can't push the
    per-task budget to infinity.

    Defaults mirror the module constants ``DEFAULT_MAX_TURNS_CEILING=250`` /
    ``DEFAULT_TIMEOUT_S_CEILING=3600`` in
    :mod:`orchestrator.budget_escalation` (``budget_escalation.py:62-66``),
    so behaviour is byte-identical to today until huge-repo scaling kicks
    in (the ``"max_turns_ceiling"`` key in
    ``task_overrides.huge_repo_multipliers`` lifts the turns ceiling 1.5×
    on huge repos; the timeout ceiling is left un-scaled).
    """

    model_config = ConfigDict(extra="forbid")

    max_turns_ceiling: int = Field(default=250, ge=1)
    timeout_s_ceiling: int = Field(default=3600, ge=1)
    # RECOVERY-CONTRACT §7 Step 8 (the A4 root cause): inclusive char-length
    # cutoff above which an ``error_max_turns`` failure is classified as
    # ``OVERSIZED_INPUT`` rather than ``GUARDRAIL_EXCEEDED``. An oversized-input
    # cause routes to BOUND_INPUT (re-dispatch with reduced scope) and does NOT
    # widen the turn budget — granting more turns just burns budget re-reading
    # the same bloat. The 200_000-char default ≈ ~50K tokens, a deliberately
    # high floor so only genuine context-window bloat trips it (a normal
    # developer/reviewer prompt is far smaller). Set higher to disable in
    # practice; set lower to bound aggressively on a constrained model.
    oversized_input_char_threshold: int = Field(default=200_000, ge=1)


class AdaptersConfig(BaseModel):
    """v0.36.0 F2: adapter-level knobs (network probes today).

    ``probe_retry_attempts`` and ``probe_backoff_initial_s`` govern the
    Claude Code adapter's ``_pong_probe`` retry loop. Backoff doubles
    on each attempt (1s → 3s → 9s by default). Set
    ``probe_retry_attempts=1`` to disable retries entirely (legacy
    single-shot behaviour).
    """

    model_config = ConfigDict(extra="forbid")

    probe_retry_attempts: int = Field(default=3, ge=1, le=10)
    probe_backoff_initial_s: float = Field(default=1.0, gt=0.0)
    # v0.39.0 B2: per-attempt PONG preflight-probe timeout in seconds.
    # The default 10.0 preserves the legacy fail-fast "is the CLI alive?"
    # behaviour; huge repos scale it 1.5× (→ 15s) via the
    # ``"probe_timeout_s"`` key in ``task_overrides.huge_repo_multipliers``
    # so the probe survives the slower cold start without operator tuning.
    probe_timeout_s: float = Field(default=10.0, ge=5.0, le=60.0)
    # v0.39.0 (huge-repo follow-up): model used for the PONG preflight
    # probe. The probe is a trivial "is the CLI alive + authed?" round-trip
    # — it does NOT need the heavy default model, whose cold start (~9-11s)
    # straddles the 10s probe timeout on a busy huge-repo startup. A fast
    # model ("haiku") cuts the cold start to ~7-8s, comfortably under the
    # timeout. Passed as ``--model <probe_model>`` in the probe command.
    # Empty string → flag omitted (CLI inherits its default model — the
    # legacy pre-fast-model behaviour).
    probe_model: str = "haiku"
    # v0.39.0 B1: spawn-agent isolation. When True, spawned headless
    # ``claude -p`` agents are isolated from the *target* repo's
    # project/local settings (SessionStart hooks) and MCP servers via
    # ``--setting-sources user --strict-mcp-config --mcp-config
    # '{"mcpServers":{}}'`` — the target's ``CLAUDE.md`` still loads (it is
    # not a settings source), so agents keep following repo conventions.
    # These are AutoDev's own single-shot workers (instructions via
    # ``--prompt``, tools via ``--allowed-tools``); they don't need the
    # target's interactive tooling, and isolating them removes the
    # cold-start latency that otherwise blows the probe timeout. Operators
    # who *want* the target's hooks/MCP in spawned agents can set False.
    suppress_target_repo_config: bool = Field(default=True)


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
    # v0.35.0 C3: bumped 3 → 10. Zero-success entries previously
    # promoted at confirmations=3 polluted the hive with anti-patterns
    # that the swarm-tier dedup couldn't catch (entries differing
    # enough on text to dodge the 0.6 Jaccard threshold). The
    # promotion gate now also requires ``succeeded_after_count > 0``
    # (enforced in :meth:`state.knowledge.KnowledgeStore._promote_if_qualified`).
    promotion_min_confirmations: int = 10
    promotion_min_confidence: float = 0.7
    denylist_roles: list[str] = Field(
        default_factory=lambda: [
            "explorer",
            "judge",
            "critic_t",
            "architect_b",
            "synthesizer",
            # ADR-0044 lever #5: keep anti_bloat_v1 seed lessons out of the
            # framing / altitude_judge cohort (minimality suspended there).
            "framing",
            "altitude_judge",
            # ADR-0045 / ADR-0046: keep seed-pack lessons out of the intake and
            # diagnosis specialist cohorts (seed-pack isolation, mirrors framing).
            "intake_enricher",
            "intake_clarifier",
            "diagnostician",
            # ADR-0047: the resolver reasons over its own bounded action
            # vocabulary + failure context; seed-pack minimality lessons are
            # irrelevant noise for it (seed-pack isolation, mirrors framing).
            "resolver",
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
    # v0.25.1 Bug #4: minimum seconds between successive retries of the
    # same task. Prevents the resume loop from burning through retries
    # 1→2→3→4 within milliseconds when a wedged task wakes up with a
    # preserved retry_count from a prior session. Default 30.0 s;
    # configurable. Set to 0.0 to disable the guard (legacy v0.25.0
    # behavior — not recommended).
    qa_retry_min_interval_s: float = 30.0
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
    # v0.39.0 A3: ceilings for the per-task budget-escalation ladder. The
    # ``| None`` annotation matches an existing defensive
    # ``getattr(orch.cfg, "budget_escalation", None)`` read in
    # ``execute_phase.py``; the ``default_factory`` makes the model live in
    # practice (defaults mirror the ``budget_escalation.py`` module
    # constants → byte-identical to today until huge-repo scaling applies
    # the ``"max_turns_ceiling"`` multiplier).
    budget_escalation: BudgetEscalationConfig | None = Field(
        default_factory=BudgetEscalationConfig
    )
    # v0.37.0 H5: master escape hatch for all H5 large-codebase auto-
    # defaults (multiplier scaling of H1/H2/H3 caps, hallucination-guard
    # skip-list extension on huge C/C++ repos, ``AUTODEV_LANG_WEIGHT``
    # default = 0.5 on huge repos). When True,
    # :func:`orchestrator.repo_size.is_huge_repo` ALWAYS returns False
    # regardless of the actual file count, restoring pre-v0.37.0
    # behaviour. Operators who want the old small-repo defaults on a
    # huge repo set this to True. Independent of the per-tournament
    # ``TournamentPhaseConfig.huge_repo_overrides_disabled`` field
    # (which gates only the plan-tournament multi-branch fast-path).
    huge_repo_overrides_disabled: bool = False
    # v0.38.0 I1 (HK12): C/C++ language-profile fraction threshold above
    # which :func:`orchestrator.repo_size.is_huge_repo`-detected repos
    # that are also C/C++-dominant get the H5 auto-skip set applied in
    # :mod:`qa.hallucination_guard`. Lower this (e.g. ``0.5``) for
    # mixed-language codebases where shader / asm / DSL files dilute the
    # C/C++ share below the default 0.80 but the bulk of generated
    # output still lives under the same engine-shaped tree layout.
    #
    # NOT scaled by ``huge_repo_multipliers``: this is a language-share
    # fraction (∈ [0, 1]), not a budget. Multiplying by 2.5× would push
    # the threshold above the maximum permitted share and disable the
    # auto-skip on every huge repo — the opposite of the operator's
    # intent. The field is operator-tunable; multiplier scaling is not
    # the right lever here.
    huge_cpp_lang_threshold: float = Field(default=0.80, ge=0.0, le=1.0)
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
    # ADR-0044: framing/altitude phase config. ``default_factory`` is mandatory
    # so a legacy ``config.json`` lacking the field still validates under
    # ``extra="forbid"`` (the factory defaults to ``enabled=True``).
    framing: FramingPhaseConfig = Field(default_factory=_default_framing_cfg)
    # ADR-0045: intake & clarification phase config. ``default_factory`` is
    # mandatory so a legacy ``config.json`` lacking the field still validates
    # under ``extra="forbid"`` (the factory defaults to ``enabled=True``; a no-op
    # for well-formed specs). Kill-switch: ``AUTODEV_INTAKE_DISABLED=1``.
    intake: IntakePhaseConfig = Field(default_factory=_default_intake_cfg)
    # ADR-0046: diagnosis (reproduce-first) phase config. ``default_factory`` is
    # mandatory so a legacy ``config.json`` lacking the field still validates
    # under ``extra="forbid"`` (the factory defaults to ``enabled=True``,
    # bug-gated). Kill-switch: ``AUTODEV_DIAGNOSIS_DISABLED=1``.
    diagnosis: DiagnosisPhaseConfig = Field(default_factory=_default_diagnosis_cfg)
    # ADR-0047: Universal Blocker Resolver config. ``default_factory`` is
    # mandatory so a legacy ``config.json`` lacking the field still validates
    # under ``extra="forbid"`` (the factory defaults to ``enabled=True``,
    # fast-path-only). Kill-switch: ``AUTODEV_RESOLVER_DISABLED=1``.
    resolver: ResolverConfig = Field(default_factory=_default_resolver_cfg)
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
    # F-4 (field-finding): apply-time edit-scope enforcement policy. The
    # apply-time gate in :meth:`WorktreeManager.apply_patch_to_main` checks
    # the developer's ACTUAL worktree diff against the resolved edit-scope
    # (``phase.edit_scope or plan.edit_scope`` UNION the task's own
    # ``files`` / ``files_new`` / ``extended_scope``). For its entire
    # history the gate was DORMANT in the execute flow — all three
    # ``_apply_with_conflict_escalation`` call sites pass no scope — so a
    # developer diff that strayed outside the declared scope was never
    # caught at apply time (only the declaration-level pre-flight in
    # ``dag.collect_edit_scope_violations`` ran, and it checks the
    # ARCHITECT's ``task.files`` declaration, not the real diff).
    #
    # Default ``"warn"`` (advisory, NON-blocking): legitimate flows
    # routinely edit beyond the declared ``task.files`` — new helper files
    # land in ``task.files_new``; corrective tasks are minted with EMPTY
    # ``files``; ``task.files`` is an architect declaration never reconciled
    # against the real diff. A ``"block"`` default would manufacture
    # failures on correct refactors/correctives, so activation is WARN-first
    # behind this flag, and the effective scope INCLUDES ``files_new`` /
    # ``extended_scope`` to minimise false warnings. When the resolved+union
    # scope is EMPTY (e.g. an empty-``files`` corrective on a repo with no
    # declared edit_scope) the check is skipped entirely (legacy whole-repo
    # no-op) so empty-scope flows are never blocked.
    #
    #   * ``"off"``   — gate skipped (the pre-F-4 behaviour).
    #   * ``"warn"``  — compute out-of-scope files, LOG
    #     ``execute_phase.edit_scope_apply_violation`` + append a
    #     best-effort ``edit_scope_apply_violation`` ledger breadcrumb,
    #     then APPLY anyway (does NOT block).
    #   * ``"block"`` — pass the effective scope to the apply gate, which
    #     raises :class:`orchestrator.dag.EditScopeViolation` before any
    #     ``git apply`` runs (main is never half-patched).
    enforce_apply_time_edit_scope: Literal["off", "warn", "block"] = "warn"
    # v0.34.0 B2: when a sparse worktree edits a C/C++ source file, also
    # admit ``*.h``/``*.hpp``/``*.hh``/``*.hxx`` siblings in the same
    # directory so the QA gates retain include-chain context for symbol
    # resolution. Capped at
    # :data:`orchestrator.worktree.WORKTREE_HEADER_EXPANSION_CAP` paths
    # — dense include trees regress to a full checkout otherwise.
    include_headers_for_sparse: bool = True
    # F-6 (Fix 2): when a per-task worktree is sparse, also fold a small
    # curated globset of TRACKED build/test-harness files (``package.json``
    # /lockfiles, ``pyproject.toml``, ``pytest.ini``, ``conftest.py``,
    # ``Cargo.toml``, ``go.mod``, …) and the task's RELEVANT test files
    # (scoped to the package/dir tree of the cone — never a repo-wide test
    # mountain) into the sparse checkout. Without this, the QA ``test_runner``
    # gate (cwd=worktree) cannot see ``package.json``/the test files and
    # false-blocks (e.g. ``npm test`` ENOENT). Shares the
    # :data:`orchestrator.worktree.WORKTREE_HEADER_EXPANSION_CAP` bound: an
    # over-cap expansion (monorepo manifest shards / a giant test tree) bails
    # out so the cone stays sparse-for-scale. Default True.
    worktree_sparse_include_harness: bool = True
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
    # Number of worker processes used by ``IndexBuilder.build_full`` to
    # parse files in parallel (the parse stage is CPU- and GIL-bound, so
    # processes, not threads). ``0`` (default) = ``os.cpu_count() or 1``;
    # ``1`` forces the serial in-process parse. Workers feed a single
    # bulk-loading sqlite writer in the parent.
    index_build_workers: int = Field(default=0, ge=0)
    # Files per write transaction during a full bulk build. Bounds the WAL
    # so a huge repo doesn't accumulate one giant single-transaction blob
    # (the pre-parallel builder produced a ~600 MB WAL that only flushed at
    # the end). Lower it on memory-constrained hosts.
    index_build_batch_size: int = Field(default=1000, ge=1)
    # On huge repos (``RepoCapacity.is_huge``) the initial full build used
    # to take minutes, so it was spawned in a background subprocess. Now
    # that the build is parallel + bulk-loaded it is fast enough to run
    # synchronously, so the default is False (sync). Set True to restore
    # the opt-in async escape hatch: ``autodev init`` spawns the builder in
    # a background subprocess and returns immediately; the
    # ``.autodev/index.db.building`` marker signals the per-trigger
    # incremental hook to skip until the build completes.
    index_huge_repo_async_init: bool = False

    # v0.30.0 Bug 5: cross-task infrastructure-failure circuit breaker.
    # Counts adapter failures whose ``subtype`` is in
    # ``{auth_failed, rate_limited, server_error, usage_limit_hit}``
    # (``usage_limit_hit`` added in v0.31.0 Phase 2.6 to capture the
    # cursor-adapter signal that a Cursor account has hit its monthly
    # / plan cap, distinguished from per-minute ``rate_limited``)
    # over a rolling window; trips when the count reaches
    # ``circuit_breaker_threshold``
    # within ``circuit_breaker_window_s`` seconds. On trip the
    # orchestrator raises
    # :class:`tournament.errors.InfrastructureCircuitOpenError`, which
    # the existing :class:`AuthenticationFailedError` catch sites
    # (v0.29.0 Bug 7) treat identically — quarantine the in-flight
    # task, park the phase at ``review_status="paused"``, exit non-zero.
    # Defaults (3 in 60s) match the user-locked value in the v0.30.0
    # plan; raise the threshold or window for noisy networks where
    # transient 5xx bursts shouldn't kill the run.
    circuit_breaker_threshold: int = Field(default=3, ge=1)
    circuit_breaker_window_s: float = Field(default=60.0, gt=0.0)

    # v0.37.0 H3: cross-task test-diagnosis circuit. Independent of
    # ``circuit_breaker_threshold`` (which counts adapter-class
    # subtypes) so adapter flakes and test-runner flakes are tuned
    # per-stream. Real-world operator runs surfaced a cascading pattern
    # where many tasks each produced a single ``capture_failed`` test
    # diagnosis (empty stdout, null returncode); each retried once and
    # hard-failed in isolation but no cross-task signal halted the run.
    # The shared :class:`InfraFailureCircuitBreaker` now feeds a second
    # deque from the test-diag classifier; on trip it raises the same
    # :class:`InfrastructureCircuitOpenError` and the existing v0.29.0
    # quarantine catch sites handle it identically.
    test_diag_breaker_threshold: int = Field(default=3, ge=1)
    """Number of test-diagnosis infrastructure failures
    (e.g. ``capture_failed``) within ``test_diag_breaker_window_s`` that
    opens the breaker. Separate from ``circuit_breaker_threshold``
    which counts adapter-class infrastructure failures."""

    test_diag_breaker_window_s: float = Field(default=600.0, gt=0.0)
    """Rolling window for the test-diagnosis breaker. Test runs are
    slower than adapter calls so the default is wider (10 minutes vs
    60 seconds)."""

    test_diag_breaker_diagnoses: list[str] = Field(
        default_factory=lambda: ["capture_failed", "turn_budget_exhausted"]
    )
    """Which :class:`~orchestrator.test_result_classifier.TestDiagnosis`
    values count toward the test-diag breaker. ``capture_failed`` is
    always recommended; WS1 adds ``turn_budget_exhausted`` to the default so
    the cross-task circuit breaker keeps coverage of the failure mode that
    was previously (mis)classified as ``capture_failed`` — dropping it from
    the default would quietly lose that systemic-halt signal. ``runtime_crash``
    and ``collection_failed`` remain opt-in because they can be legitimate
    per-task issues."""

    treat_unrunnable_tests_as_no_tests: bool = Field(default=False)
    """When True, infrastructure-class test diagnoses (``capture_failed``,
    ``collection_failed``, ``runtime_crash``) are soft-passed like
    ``no_tests_found`` instead of triggering the per-task hard-fail. For
    environments that cannot build/run the target repo's test suite (e.g.
    an external engine repo with no local build/device), where an empty
    test capture is not a code defect. The real diagnosis is still
    recorded on ``TestEvidence.diagnosis`` for forensics. Default False
    preserves the strict behaviour."""

    # v0.41.0 (P1-F): bounded soft-pass for ``capture_failed`` specifically.
    # Observed failure: a trivial, otherwise-passing task whose test step
    # could not CAPTURE/parse a result (empty ``text``/``raw_stderr`` from the
    # ``test_engineer``, ``total==0``) looped reviewed→in_progress→reviewed and
    # exhausted its retries straight into ``blocked``. Capture failing is an
    # infrastructure problem, not a code defect, so after this many capture
    # attempts the test step SOFT-PASSES (advances the task to ``tested`` and
    # stamps ``TestEvidence.soft_passed=True`` + a reason) rather than
    # hard-failing. A real RED test (captured ``failed > 0`` → diagnosis
    # ``ok``) is never soft-passed; this knob only governs the genuinely
    # uncapturable ``capture_failed`` path. ``collection_failed`` and
    # ``runtime_crash`` carry signal and keep their retry-then-hard-fail
    # behaviour. Set to a high value to effectively disable the bounded
    # soft-pass (restores pre-v0.41.0 retry-once-then-block for
    # ``capture_failed``); cannot be < 1.
    capture_failed_soft_pass_after: int = Field(default=2, ge=1)
    """Number of consecutive ``capture_failed`` test attempts on a single
    task after which the test step soft-passes (advances to ``tested`` with
    ``TestEvidence.soft_passed=True``) instead of hard-failing to ``blocked``.
    Default ``2`` = one retry, then soft-pass on the second uncapturable
    result. Only applies to ``capture_failed`` with no captured failures;
    real failures and ``collection_failed`` / ``runtime_crash`` are
    unaffected."""

    # v0.38.0 I4: exponential backoff + auto-reset for the test-diag
    # stream. Threshold-crossing no longer hard-halts immediately —
    # the orchestrator first sleeps ``initial * (multiplier ** n)``
    # capped at ``max_s`` (mirrors the
    # :meth:`adapters.claude_code.ClaudeCodeAdapter._pong_probe` backoff
    # pattern). Only when ``cumulative_backoff_s`` crosses
    # ``test_diag_backoff_total_budget_s`` does the breaker raise
    # :class:`InfrastructureCircuitOpenError`. Auto-reset clears the
    # failure deque after ``N`` successful test runs within
    # ``window_s`` so a single flaky burst doesn't permanently arm the
    # circuit on an otherwise healthy runner.
    test_diag_backoff_initial_s: float = Field(default=5.0, ge=0.0)
    """First backoff delay (seconds) once the test-diag threshold
    crosses. ``0.0`` disables the sleep but still consumes budget."""

    test_diag_backoff_multiplier: float = Field(default=2.0, gt=1.0)
    """Exponential growth factor between successive backoffs. ``2.0``
    doubles each iteration; matches the ``_pong_probe`` 3× shape's
    spirit but tuned slower for the longer test-run cadence."""

    test_diag_backoff_max_s: float = Field(default=120.0, gt=0.0)
    """Per-iteration backoff ceiling — caps the growth before it
    consumes the entire budget on one sleep. Default 120s matches the
    operator-observed sweet spot for transient runner flakes."""

    test_diag_backoff_total_budget_s: float = Field(default=600.0, gt=0.0)
    """Cumulative backoff budget per task. When the sum of sleeps
    crosses this ceiling the breaker raises
    :class:`InfrastructureCircuitOpenError` (the hard halt). Auto-scales
    2.0× on huge repos via :attr:`huge_repo_multipliers`."""

    test_diag_auto_reset_after_n_successes: int = Field(default=3, ge=1)
    """Number of successful test runs within
    ``test_diag_auto_reset_window_s`` that clears the failure deque.
    Lets a healthy run recover from a prior flaky burst without
    operator intervention."""

    test_diag_auto_reset_window_s: float = Field(default=900.0, gt=0.0)
    """Rolling window for the auto-reset success counter. 15 minutes
    by default — wide enough that a slow phase's successive successes
    accumulate, narrow enough that ancient successes don't artificially
    clear a fresh burst. Auto-scales 2.0× on huge repos via
    :attr:`huge_repo_multipliers`."""

    # v0.38.0 I4 (HK6): drain timeout for the cross-task / cross-phase
    # parallel pool when a typed halt cancels in-flight workers. Before
    # I4 the halt path called ``asyncio.gather`` unbounded, which let
    # slow-teardown adapters (real-world enterprise runs) stall the
    # whole process for ~30s after the trip. With the timeout the
    # drainer cancels, waits up to ``drain_timeout_s``, then forces
    # ``return_exceptions=True`` on any straggler so ``CancelledError``
    # doesn't propagate.
    parallel_pool_drain_timeout_s: float = Field(default=10.0, gt=0.0)
    """Max seconds to wait for in-flight parallel workers to cancel
    after a typed halt (``AuthenticationFailedError`` /
    ``InfrastructureCircuitOpenError``). Stragglers past the timeout
    are absorbed via ``gather(return_exceptions=True)`` and logged."""

    # v0.37.0 H1: per-kind tail cap (in characters) for the reviewer /
    # test / coder ``raw_response`` bodies that
    # :func:`orchestrator.execute_phase._build_recent_evidence_block`
    # folds into the ``recent_evidence`` block sent to stuck-recovery
    # prompts (architect-consult, critic_sounding_board). Set to ``0`` to
    # disable evidence-body inclusion and restore the legacy one-liner
    # behaviour for operators on tight token budgets.
    recent_evidence_max_chars_per_kind: int = Field(default=4000, ge=0)
    # v0.37.0 H1: which evidence kinds to fold into the ``recent_evidence``
    # block. Order is preserved in the rendered prompt. ``"developer"`` is
    # the canonical label matching the on-disk
    # :class:`state.schemas.CoderEvidence.kind` discriminator and the
    # rendered ``DEVELOPER_RAW:`` section header. An empty list restores
    # the legacy one-liner behaviour even when the per-kind cap is
    # non-zero.
    #
    # v0.38.0 HK1 (soft breaking change): legacy ``"coder"`` is accepted
    # by the validator below and rewritten to ``"developer"`` with a
    # one-shot ``config.deprecated_kind_label`` warning. The shim ships
    # for v0.38.x only and is scheduled for removal in v0.39.0.
    recent_evidence_include_kinds: list[str] = Field(
        default_factory=lambda: ["review", "test", "developer"]
    )

    # v0.37.0 H2: cumulative cap on correction tasks per phase across all
    # corrective rounds (architect-refine + phase-review tournament). The
    # orchestrator computes each phase's remaining budget upstream and
    # threads it into :func:`orchestrator.corrective_parser.parse_corrective_direction`
    # via ``max_tasks``. When the budget is exhausted the configured
    # :attr:`corrective_cap_action` fires. Default ``8`` matches the
    # observed natural ceiling on normal-sized repos; raise on projects
    # where legitimate large refactors routinely need more, or rely on
    # the H5 huge-repo auto-bump for codebases past the probe threshold.
    max_corrective_tasks_per_phase: int = Field(default=8, ge=1)
    # v0.38.0 I3: cumulative cap on correction tasks across ALL phases of
    # a plan. Mirrors :attr:`max_corrective_tasks_per_phase` at plan scope
    # to prevent multi-phase corrective accumulation. Default 24 ≈
    # 8 × 3 phases of headroom. The orchestrator computes the plan-scope
    # remaining budget at both corrective-injection sites (architect-refine
    # and phase-review) and feeds ``min(per_phase_remaining,
    # per_plan_remaining)`` into the parser; when the plan-scope cap is
    # the binding ceiling, the originating task soft-blocks (or skip-
    # rounds, per :attr:`corrective_cap_action`) and the ledger op
    # ``corrective_cap_reached`` carries ``scope="plan"`` so dashboards
    # can distinguish the two ceilings. Auto-scales 2.0× on huge repos
    # via :attr:`huge_repo_multipliers`.
    max_corrective_tasks_per_plan: int = Field(default=24, ge=1)
    # v0.37.0 H2: behaviour when a phase reaches its corrective-task
    # budget. ``soft_block_phase`` (default) routes the originating task
    # to operator triage via the recovery-hint pipeline so the
    # ``autodev status --blocked`` panel surfaces the cap-hit with an
    # actionable next-step. ``skip_corrective_round`` drops the round
    # silently and continues — useful for unattended fleets where
    # operator intervention is unavailable and the safer default is to
    # let the phase land on whatever was completed.
    corrective_cap_action: Literal[
        "soft_block_phase", "skip_corrective_round"
    ] = "soft_block_phase"

    # v0.36.0 F2: adapter-level network probe retry knobs.
    adapters: AdaptersConfig = Field(default_factory=AdaptersConfig)

    # v0.37.0 H4: when True (default), trigger-context env detection
    # (``CLAUDECODE=1`` / ``CLAUDE_PROJECT_DIR`` for Claude Code,
    # ``TERM_PROGRAM=Cursor`` / ``CURSOR_*`` for Cursor) overrides the
    # ``AUTODEV_PLATFORM`` env in ``preferred="auto"`` mode so that
    # ``autodev`` invoked from inside a Claude Code session selects the
    # ``claude_code`` adapter, and likewise for Cursor. Explicit
    # ``--platform X`` always wins. Set to False to restore the
    # pre-v0.37.0 precedence (env beats host context).
    adapter_respect_trigger_context: bool = Field(default=True)

    # v0.38.0 HK9: explicit allowlist for Cursor shell env vars treated
    # as a trigger-context signal in :func:`adapters.detect._detect_trigger_context`.
    # The built-in allowlist already covers ``CURSOR_TRACE_ID`` /
    # ``CURSOR_AGENT`` / ``CURSOR_VERSION`` / ``CURSOR_AGENT_ID``; this
    # field lets operators on newer Cursor versions (which may set
    # additional vars) extend the allowlist without waiting for a
    # release. Replaces the v0.37.0 ``startswith("CURSOR_")`` heuristic
    # which over-matched on shell rc files like ``CURSOR_RC_FILE``.
    cursor_trigger_env_extra: list[str] = Field(default_factory=list)

    # v0.38.0 HK3: when True (default), write a JSON envelope of every
    # ARCHITECT_CONSULT dispatch to ``.autodev/debug/architect_consult-*.json``
    # so post-mortems can reconstruct what the architect saw without
    # tailing the orchestrator stdout. Forensics is cheap; default on.
    dump_architect_consult_envelopes: bool = Field(default=True)

    @model_validator(mode="after")
    def _migrate_inline_platform(self) -> "AutodevConfig":
        """v0.26.0: rewrite legacy ``platform: "inline"`` to ``"claude_code"``.

        InlineAdapter was removed in v0.26.0. Existing workspaces with
        ``.autodev/config.json`` carrying ``platform: "inline"`` would
        otherwise be invalid on load. The Literal still includes
        ``"inline"`` for one release so configs validate; this validator
        emits a :class:`DeprecationWarning` and rewrites the field to
        ``"claude_code"``. Scheduled for hard-removal in v0.27.0.
        """
        if self.platform == "inline":
            import warnings

            warnings.warn(
                "platform: 'inline' is deprecated in v0.26.0 and will "
                "be removed in v0.27.0. Treated as 'claude_code'. "
                "Update .autodev/config.json or run "
                "`autodev init --force` to refresh the workspace.",
                DeprecationWarning,
                stacklevel=2,
            )
            self.platform = "claude_code"
        return self

    @model_validator(mode="after")
    def _migrate_recent_evidence_kinds(self) -> "AutodevConfig":
        """v0.38.0 HK1 (soft breaking change): rewrite legacy ``"coder"``
        in :attr:`recent_evidence_include_kinds` to ``"developer"``.

        The user-facing label diverged from the on-disk
        :class:`state.schemas.CoderEvidence.kind` discriminator
        (``"developer"``) for one release. Unifying the names lets
        operators map their config entries directly to evidence files
        without an indirection table. A one-shot
        ``config.deprecated_kind_label`` warning fires per legacy value
        so the rewrite is visible in non-interactive runs. Scheduled
        for hard-removal in v0.39.0.
        """
        if not self.recent_evidence_include_kinds:
            return self
        rewritten = []
        legacy_seen = False
        for kind in self.recent_evidence_include_kinds:
            if kind == "coder":
                rewritten.append("developer")
                legacy_seen = True
            else:
                rewritten.append(kind)
        if legacy_seen:
            _emit_kind_deprecation_warning()
            self.recent_evidence_include_kinds = rewritten
        return self

    def require_all_roles(self) -> None:
        """Raise ValueError if any required role is missing from `agents`."""
        missing = [r for r in REQUIRED_AGENT_ROLES if r not in self.agents]
        if missing:
            raise ValueError(f"missing required agent roles: {missing}")
