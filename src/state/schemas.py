"""Pydantic v2 schemas for plan and evidence structures.

The schema models:
  - Plan/Phase/Task for the execution plan
  - Evidence discriminated union for QA gate results

The autodev schema differs from the original in three ways:

1. ``TaskStatus`` extends the original set to cover the richer FSM described in
   section C of the plan: ``coded``, ``auto_gated``, ``reviewed``, ``tested``,
   ``tournamented``, ``complete``, ``skipped``.
2. Phases and Tasks are string-keyed (``"1"``, ``"1.1"``) rather than numeric
   — mirrors architect markdown output.
3. The evidence discriminator is ``kind`` (not ``type``) to match autodev's
   internal convention and avoid shadowing Python's ``type`` keyword.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _validate_edit_scope_paths(scope: list[str]) -> list[str]:
    """Normalize and validate a list of repo-relative path prefixes.

    v0.14.0: ``Plan.edit_scope`` and ``Phase.edit_scope`` use repo-relative
    path prefixes for substring/prefix matching downstream. This validator:

    * trims trailing ``/`` so ``"src/"`` and ``"src"`` are equivalent;
    * rejects absolute paths (``"/etc/passwd"``) — scope is repo-relative;
    * rejects paths containing ``..`` segments — would break the
      is_in_scope semantics by escaping the repo root.

    Empty list is the no-op (legacy) value and is returned unchanged.
    """
    out: list[str] = []
    for raw in scope:
        if not isinstance(raw, str):
            raise ValueError(f"edit_scope entries must be str, got {type(raw).__name__!r}")
        if raw.startswith("/"):
            raise ValueError(
                f"edit_scope entries must be repo-relative, got absolute path {raw!r}"
            )
        # Reject ``..`` as a standalone segment (split on '/'), not as a
        # substring — paths like ``src/some..file`` should be allowed; only
        # parent-directory traversal is rejected.
        parts = raw.split("/")
        if any(p == ".." for p in parts):
            raise ValueError(
                f"edit_scope entries cannot contain '..' segments, got {raw!r}"
            )
        out.append(raw.rstrip("/"))
    return out


TaskStatus = Literal[
    "pending",
    "in_progress",
    "coded",
    "auto_gated",
    "reviewed",
    "tested",
    "tournamented",
    "complete",
    "blocked",
    "skipped",
    # v0.29.0 Bug 7: non-terminal halt state for tasks stopped by an
    # infrastructure failure (e.g. ``AuthenticationFailedError``). Unlike
    # ``blocked``, ``quarantined`` is NOT in the terminal set used by
    # depends_on satisfaction or phase-aggregate checks — the task remains
    # eligible for re-execution and ``Orchestrator.resume()`` picks it up
    # automatically once the operator clears the underlying infra issue.
    "quarantined",
]
"""Allowed states for a :class:`Task`. See :mod:`orchestrator.task_state`."""


class CriterionVote(BaseModel):
    """A single judge vote on an acceptance criterion (v0.18.0 C2).

    Recorded by the impl-tournament runner when ``voting_strategy=veto``
    so post-hoc analysis can see how each judge evaluated each criterion
    across passes — the criterion-evolution log.
    """

    model_config = ConfigDict(extra="forbid")

    judge_role: str
    verdict: Literal["APPROVE", "REJECT", "ABSTAIN"]
    justification: str = ""
    timestamp: str = ""


class AcceptanceCriterion(BaseModel):
    """A single check-box acceptance criterion attached to a task."""

    model_config = ConfigDict(extra="forbid")

    id: str
    description: str
    met: bool = False
    # v0.18.0 C2: optional per-criterion vote history. The impl-tournament
    # runner appends one :class:`CriterionVote` per judge per pass when
    # ``voting_strategy=veto``. Used as the criterion-evolution log for
    # forensics and as a Tier-1 input for future per-criterion model
    # routing (v0.20.0+). Empty list (default) preserves prior behavior.
    vote_history: list[CriterionVote] = Field(default_factory=list)


class Task(BaseModel):
    """Leaf unit of work that the ``coder`` role implements.

    v0.17.0 S5: ``files`` accepts glob patterns (``*``, ``?``, ``[...]``).
    Downstream consumers (:func:`orchestrator.dag.find_file_overlaps`,
    :func:`orchestrator.dag.validate_edit_scope`) expand globs against a
    tracked-files cache. The schema validator here permits both literal
    paths and glob entries without distinguishing — runtime expansion
    handles the cache lookup.
    """

    model_config = ConfigDict(extra="forbid")

    id: str  # "1.1", "1.2", "2.1" — phase.sequence
    phase_id: str  # "1", "2", "3"
    title: str
    description: str
    status: TaskStatus = "pending"
    files: list[str] = Field(default_factory=list)
    # v0.24.3: paths the task itself will create. Parser strips a ``[new]``
    # prefix off ``Files:`` entries and routes them here so the v0.24.3
    # ``validate_files_exist`` check can skip them during the on-disk
    # existence sweep. Empty list is the back-compat default — v0.24.2
    # ``plan.json`` files deserialize cleanly with ``files_new=[]`` and
    # behave identically to pre-v0.24.3 plans.
    files_new: list[str] = Field(default_factory=list)
    # v0.20.0 C1: per-task additional path prefixes the task may modify on
    # top of ``Phase.edit_scope`` / ``Plan.edit_scope``. Architects emit
    # these via the ``Extended-scope:`` block when a task legitimately
    # needs to touch files outside the declared scope (e.g. a refactor
    # that crosses a single auxiliary module). Non-empty values are
    # subject to the v0.20.0 C2 critic-review pre-validation flow before
    # ``validate_edit_scope`` admits the paths. Empty list (default)
    # preserves byte-identical v0.19.0 behavior.
    #
    # Validators mirror :attr:`Plan.edit_scope` /
    # :attr:`Phase.edit_scope`: trim trailing ``/``, reject absolute
    # paths, reject ``..`` segments. Repo-relative path prefixes only.
    extended_scope: list[str] = Field(default_factory=list)

    @field_validator("extended_scope", mode="after")
    @classmethod
    def _validate_extended_scope(cls, v: list[str]) -> list[str]:
        """v0.20.0 C1: same validators as Plan/Phase ``edit_scope``."""
        return _validate_edit_scope_paths(v)

    @field_validator("files", "files_new", mode="after")
    @classmethod
    def _validate_files_format(cls, v: list[str]) -> list[str]:
        """Permit glob entries; reject only structurally bogus paths.

        v0.17.0 S5: an architect may declare ``files: ["src/qa/*.py"]``.
        The validator does not at this layer attempt to resolve globs
        against the project — that's the runtime expansion's job. Here
        we just enforce that every entry is a non-empty string and is
        repo-relative (no leading ``/``, no ``..`` segments). Same shape
        as :func:`_validate_edit_scope_paths` so the surface is uniform.
        """
        for raw in v:
            if not isinstance(raw, str) or not raw:
                raise ValueError(
                    f"Task.files entries must be non-empty strings, got {raw!r}"
                )
            if raw.startswith("/"):
                raise ValueError(
                    f"Task.files entries must be repo-relative, got absolute "
                    f"path {raw!r}"
                )
            parts = raw.split("/")
            if any(p == ".." for p in parts):
                raise ValueError(
                    f"Task.files entries must not contain '..' segments, "
                    f"got {raw!r}"
                )
        return v
    acceptance: list[AcceptanceCriterion] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    requires: list[Literal["hardware", "human", "external_service", "manual"]] = Field(
        default_factory=list,
        description=(
            "Tokens marking the task as non-agent-executable. "
            "Non-empty values cause execute_phase to skip the task."
        ),
    )
    # Architect-emitted per-task complexity bucket parsed from the body line
    # ``- Complexity: simple|medium|complex`` (see plan_parser._RE_TASK_COMPLEXITY).
    # ``None`` for legacy plans (no Complexity line) or when the architect
    # hasn't emitted it for a task — the orchestrator's task_overrides
    # resolver returns ``None`` and execute_phase falls back to the spec
    # default. Distinct enum from :class:`Plan.complexity` (plan-level rollup
    # used for tournament effort) which uses the same three buckets but is
    # set from the trailing ``COMPLEXITY:`` directive.
    complexity: Literal["simple", "medium", "complex"] | None = None
    retry_count: int = 0
    # v0.25.1 Bug #4: ISO-8601 UTC timestamp of the most recent
    # ``mark_task_retry``. Persisted in the ledger so ``autodev resume``
    # can enforce ``qa_retry_min_interval_s`` across sessions instead of
    # burning through the retry budget within milliseconds. ``None``
    # before the first retry; older ledgers (pre-v0.25.1) restore as
    # ``None`` (backward-compatible default).
    last_retry_at: str | None = None
    escalated: bool = False
    assigned_agent: str | None = None  # usually "developer"
    evidence_bundle: str | None = None  # path (relative to repo root) to evidence json
    blocked_reason: str | None = None
    # v0.29.0 Bug 6: typed category for the block. ``None`` for backward
    # compatibility with on-disk plans written before v0.29.0 — the
    # ``PlanManager`` load shim backfills the field by classifying the
    # ``blocked_reason`` string with the same keyword heuristic the
    # ``autodev requeue --infrastructure`` selector uses (see
    # :mod:`state.infra_patterns`). New blocks stamp the class explicitly
    # at every block site in :mod:`orchestrator.execute_phase` and
    # :meth:`PlanManager.mark_blocked_descendants`. Three buckets:
    #
    #   * ``"verdict"``        — agent reached a legitimate negative
    #     verdict (reviewer rejected, tests failed past retry, etc.).
    #   * ``"infrastructure"`` — outside-the-loop transient failure
    #     (auth refresh, gateway 4xx, network, timeout). Safely
    #     requeueable once the operator fixes the environment.
    #   * ``"cap"``            — agent legitimately ran out of turns /
    #     tokens / budget. Distinct from ``"infrastructure"`` because
    #     requeueing without widening the cap would just re-burn it.
    block_reason_class: Literal["verdict", "infrastructure", "cap"] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    # v0.27.0 (audit §6): does this task's developer attempt produce a
    # unified diff? Tasks that legitimately produce no diff (pure
    # investigation, doc-only review, etc.) set this to ``False`` so the
    # diff-scoped QA gate site skips its fail-closed check. Default
    # ``True`` matches pre-v0.27 behaviour: every task is treated as a
    # diff-producing task until the architect opts out via the
    # ``produces_diff: false`` body line in the plan markdown.
    produces_diff: bool = True


class Phase(BaseModel):
    """Group of tasks delivered together."""

    model_config = ConfigDict(extra="forbid")

    id: str  # "1", "2", "3"
    title: str
    description: str = ""
    tasks: list[Task]
    # v0.9.0: per-phase acceptance criteria for the phase-review tournament.
    # Parsed from the architect markdown's ``- Acceptance:`` block placed
    # directly under each ``## Phase N:`` header (mirrors task-level
    # acceptance shape). Empty list for legacy plans (no phase-level
    # Acceptance block) — the judge prompt gracefully falls back to
    # evaluating against the task list and phase title in that case.
    acceptance: list[AcceptanceCriterion] = Field(default_factory=list)
    # v0.9.0: HEAD commit at phase entry, captured by execute_phase before
    # the first task in the phase begins. Used as the ``from_sha`` of
    # ``_git_diff_range`` when the phase-review tournament builds the
    # PhaseReviewBundle. ``None`` for legacy plans / phases that haven't
    # started executing yet.
    baseline_commit: str | None = None
    # v0.21.0 B1: HEAD commit captured at phase-completion checkpoint —
    # the moment ALL tasks in the phase reach a terminal state. Distinct
    # from the live HEAD because, with cross-phase parallelism enabled,
    # the next phase's tasks may start landing commits before the
    # phase-review tournament runs. The phase-review runner uses this
    # field as the ``tip_commit`` of its diff range so the as-implemented
    # diff captures only this phase's work even when phase N+1 runs
    # concurrently. ``None`` for legacy plans / phases that haven't
    # finished yet.
    end_checkpoint_commit: str | None = None
    # v0.9.0: phase-review state machine. ``None`` (initial) →
    # ``"in_progress"`` (when the tournament starts) → ``"accepted"`` |
    # ``"corrective_required"`` | ``"skipped"`` (terminal). The orchestrator
    # uses this as a critical loop guard: once a phase has been reviewed,
    # the next observation of all-terminal task state does NOT re-fire the
    # tournament. Corrective tasks landing terminal transition the status
    # from ``"corrective_required"`` → ``"accepted"`` directly.
    # v0.29.0 Bug 7: ``"paused"`` is a non-terminal review state set by
    # the phase aggregator when one or more tasks in the phase are in the
    # new ``quarantined`` task state. The aggregator refuses to fire the
    # phase-review tournament on a partial / halted phase; instead it
    # parks the phase here so :meth:`Orchestrator.resume` can re-trigger
    # the review once the quarantined tasks resolve.
    review_status: (
        Literal[
            "pending",
            "in_progress",
            "accepted",
            "corrective_required",
            "skipped",
            "paused",
        ]
        | None
    ) = None
    # v0.9.0: ids of corrective tasks injected by ``parse_corrective_direction``
    # after a B/AB-winner phase review. Mirrors the role of ``Task.depends_on``
    # but at phase scope — observability for "which sub-tasks were synthesized
    # from architect_b's direction text vs. the architect's original plan".
    corrective_task_ids: list[str] = Field(default_factory=list)
    # v0.14.0: optional per-phase override of ``Plan.edit_scope``. ``None``
    # (default) means "inherit Plan.edit_scope" — distinct from the empty
    # list, which would explicitly opt into legacy whole-repo semantics for
    # this phase only. When non-None, validators mirror those on
    # ``Plan.edit_scope`` (trim trailing slash, reject absolute, reject
    # ``..`` segments).
    edit_scope: list[str] | None = None

    @field_validator("edit_scope", mode="after")
    @classmethod
    def _validate_phase_edit_scope(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        return _validate_edit_scope_paths(v)


class Plan(BaseModel):
    """Top-level plan produced by ``architect`` (optionally refined by PlanTournament)."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str
    spec_hash: str
    phases: list[Phase]
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Architect-emitted plan-complexity bucket parsed from the trailing
    # ``COMPLEXITY: simple|medium|complex`` line of the plan markdown.
    # ``None`` for legacy plans (no COMPLEXITY: line) or when the architect
    # hasn't emitted it yet — the effort resolver gracefully falls back to
    # the user-global Claude Code default for non-architect roles in that
    # case. Distinct enum from ``AutodevConfig.user_complexity`` which uses
    # {low, medium, high, max}.
    complexity: Literal["simple", "medium", "complex"] | None = None
    created_at: str
    updated_at: str
    content_hash: str = ""  # CAS hash, recomputed on save by the ledger
    # v0.14.0: optional list of repo-relative path prefixes the plan may
    # modify. Empty list (default) preserves legacy whole-repo behavior —
    # no constraint is enforced. When non-empty, the orchestrator's
    # :func:`orchestrator.dag.validate_edit_scope` ensures every task's
    # ``files`` are subsumed by the resolved scope before any task
    # dispatches. Validator: trim trailing ``/``, reject absolute paths,
    # reject paths containing ``..`` segments. Phase-level override lives
    # on :attr:`Phase.edit_scope` (``None`` = inherit; non-None = override).
    edit_scope: list[str] = Field(default_factory=list)

    @field_validator("edit_scope", mode="after")
    @classmethod
    def _validate_plan_edit_scope(cls, v: list[str]) -> list[str]:
        return _validate_edit_scope_paths(v)


# ---------------------------------------------------------------------------
# Evidence discriminated union (discriminator field: "kind")
# ---------------------------------------------------------------------------


class _BaseEvidence(BaseModel):
    """Common fields — every evidence variant carries ``task_id``."""

    model_config = ConfigDict(extra="forbid")

    task_id: str


class CoderEvidence(_BaseEvidence):
    """Artifact produced by the ``developer`` role."""

    kind: Literal["developer"] = "developer"
    diff: str | None = None
    files_changed: list[str] = Field(default_factory=list)
    output_text: str = ""
    duration_s: float = 0.0
    success: bool = True
    # v0.31.0 (Phase 1.2): preserve the agent's raw response text even
    # when ``output_text`` is empty / parsed-down to nothing. Lets
    # post-mortems answer "what did the model actually return?" without
    # having to grep ``.autodev/debug/*-empty.json`` dumps. Optional for
    # backward compat with evidence files written before this field
    # existed.
    raw_response: str | None = None


class ReviewEvidence(_BaseEvidence):
    """Artifact produced by the ``reviewer`` role."""

    kind: Literal["review"] = "review"
    # v0.31.0 (Phase 1.3): ``MALFORMED`` is a NEW value distinct from
    # ``NEEDS_CHANGES`` — it signals "the parser could not extract a
    # verdict from the response", a machinery failure rather than a
    # legitimate negative review. The orchestrator treats the two
    # differently (NEEDS_CHANGES is a content signal that retries with
    # the same prompt; MALFORMED is a format signal that warrants a
    # stricter reminder + a debug dump).
    verdict: Literal["APPROVED", "NEEDS_CHANGES", "REJECTED", "MALFORMED"]
    issues: list[str] = Field(default_factory=list)
    output_text: str = ""
    # v0.31.0 (Phase 1.2): see ``CoderEvidence.raw_response``.
    raw_response: str | None = None


class TestEvidence(_BaseEvidence):
    """Artifact produced by the ``test_engineer`` role."""

    # Suppress pytest's attempt to collect this as a test class; the ``Test``
    # prefix is a schema naming choice, not a test marker.
    __test__ = False

    kind: Literal["test"] = "test"
    passed: int = 0
    failed: int = 0
    total: int = 0
    output_text: str = ""
    coverage_pct: float | None = None
    # v0.31.0 (Phase 1.2): see ``CoderEvidence.raw_response``.
    raw_response: str | None = None


class ExploreEvidence(_BaseEvidence):
    """Artifact produced by the ``explorer`` role during the plan phase."""

    kind: Literal["explore"] = "explore"
    findings: str
    files_referenced: list[str] = Field(default_factory=list)


class SMEEvidence(_BaseEvidence):
    """Artifact produced by the ``domain_expert`` role during the plan phase."""

    kind: Literal["domain_expert"] = "domain_expert"
    topic: str = ""
    findings: str
    confidence: Literal["HIGH", "MEDIUM", "LOW"] = "MEDIUM"


class CriticEvidence(_BaseEvidence):
    """Artifact produced by the ``critic`` role (plan-gate or sounding-board)."""

    kind: Literal["critic"] = "critic"
    verdict: Literal["APPROVED", "NEEDS_REVISION", "REJECTED"]
    issues: list[str] = Field(default_factory=list)
    output_text: str = ""


class TournamentEvidence(_BaseEvidence):
    """Summary of a tournament run (plan or impl) for a task.

    Written after :class:`Tournament` / :class:`ImplTournament` completes;
    mirrors the disk artifacts under ``.autodev/tournaments/{tournament_id}/``
    but lives in evidence for quick status queries and hive promotion.
    """

    kind: Literal["tournament"] = "tournament"
    tournament_id: str
    # v0.9.0: extended to include ``"phase_review"`` for the per-phase code
    # review tournament. Backward compat: existing evidence files load via
    # the discriminator without migration (the field is parsed strictly so
    # legacy values ``"plan"`` / ``"impl"`` continue to validate).
    phase: Literal["plan", "impl", "phase_review"]
    passes: int
    winner: Literal["A", "B", "AB"]
    converged: bool
    history: list[dict[str, Any]] = Field(default_factory=list)
    final_diff: str | None = None


class ReviewCandidate(BaseModel):
    """One A / B / AB candidate inside a :class:`ReviewTournamentEvidence`.

    v0.32.0 Phase 2: the review tournament produces three candidates per
    refinement round — A is the unchanged developer patch + original
    reviewer verdict, B is the adversarial reviewer's alternative
    assessment, AB is the merge synthesizer's resolution. Each candidate
    carries a verdict + issues (the same shape :class:`ReviewEvidence`
    persists for the single-shot reviewer path) plus a short diff
    excerpt for forensics, so post-mortems can answer "what did the
    judges actually rank against each other?" without crossing into the
    tournament's on-disk artifact tree.
    """

    model_config = ConfigDict(extra="forbid")

    diff_excerpt: str = ""
    """First N bytes of the developer diff the candidate's review was
    grounded against. All three candidates share the SAME diff excerpt
    inside one tournament round — the difference between A / B / AB is
    in the *review* of the diff, not the diff itself."""

    verdict: str = "MALFORMED"
    """Same value space as :class:`ReviewEvidence.verdict`:
    ``APPROVED``, ``NEEDS_CHANGES``, ``REJECTED``, ``MALFORMED``.
    Typed as ``str`` so future verdict tokens (e.g. ``CONDITIONAL``)
    can be added by the prompts without a schema migration; the
    runner validates each value against the reviewer parser at write
    time."""

    issues: list[str] = Field(default_factory=list)
    """Bullet-point issues extracted from the candidate's response by
    :func:`orchestrator.execute_phase._parse_review_verdict`."""

    raw_response: str | None = None
    """v0.31.0 Phase 1.2 parity: preserve the agent's raw response text
    so post-mortems can answer "what did the model actually return?"
    without depending on the on-disk tournament artifact tree."""


class ReviewTournamentEvidence(_BaseEvidence):
    """Summary of a review-tournament run (v0.32.0 Phase 2).

    Distinct from :class:`TournamentEvidence` because:

    * the phase is ALWAYS ``"review"`` (no plan / impl / phase_review
      mix-up — the discriminator pins it);
    * the candidate set is fixed at 3 (A / B / AB) — encoded as a
      ``dict[str, ReviewCandidate]`` keyed on the canonical labels so
      readers can deref by label without indexing magic;
    * judge rankings are persisted as a list-of-lists (one entry per
      judge, in cohort order) with ``None`` for parse failures —
      matches the wire format
      :func:`tournament.voting.BordaAggregator.aggregate` consumes;
    * ``borda_scores`` records the aggregated point totals per label
      so forensics can see *how close* the win was without re-running
      the tally.

    Written by :func:`orchestrator.review_tournament_runner.run_review_tournament`
    after the loop converges or hits ``max_rounds``; a parallel ledger
    breadcrumb (``review_tournament_converged`` / ``..._escalated``)
    points back to this evidence file.
    """

    kind: Literal["review_tournament"] = "review_tournament"
    tournament_id: str
    candidates: dict[str, ReviewCandidate] = Field(default_factory=dict)
    """Keyed on the canonical labels ``"A"``, ``"B"``, ``"AB"``. The
    runner ALWAYS writes all three keys even when one candidate is
    structurally identical to another (forensic completeness — the
    no-progress detector reads the dict and decides what to do)."""

    judge_rankings: list[list[str] | None] = Field(default_factory=list)
    """One entry per judge in cohort order; each entry is a list of
    canonical labels in best→worst order, or ``None`` for parse
    failures. The list length matches the cohort size at write time
    (``cfg.review_num_judges`` or the override length)."""

    winner: str = "A"
    """Borda winner. ``"A"`` on a tie via the conservative
    ``tiebreak_winner="A"`` invariant."""

    borda_scores: dict[str, int] = Field(default_factory=dict)
    """Aggregated Borda points per label. Sum equals
    ``valid_judges * len(labels)`` when all judges parsed cleanly —
    see ``test_borda_score_invariant`` for the property test."""

    valid_judges: int = 0
    """Number of judges whose ranking parsed cleanly (``ranking is not
    None``). Distinct from cohort size — a cohort of 3 with one
    MALFORMED judge has ``valid_judges == 2``."""

    converged: bool = False
    """True iff the loop terminated via the ``convergence_k`` A-streak
    rule (do-nothing semantics — the original verdict stood). False
    when the loop exited via ``max_rounds`` or a B / AB winner."""

    rounds: int = 1
    """Number of refinement rounds the loop ran. ``1`` for the
    happy-path single-pass A win; ``cfg.review_max_rounds`` on the
    escalation path."""


Evidence = Annotated[
    Union[
        CoderEvidence,
        ReviewEvidence,
        TestEvidence,
        ExploreEvidence,
        SMEEvidence,
        CriticEvidence,
        TournamentEvidence,
        ReviewTournamentEvidence,
    ],
    Field(discriminator="kind"),
]
"""Discriminated union of every evidence variant. Use ``TypeAdapter(Evidence)``
to round-trip a ``dict`` into the correct subclass.
"""


__all__ = [
    "AcceptanceCriterion",
    "CoderEvidence",
    "CriterionVote",
    "CriticEvidence",
    "Evidence",
    "ExploreEvidence",
    "Phase",
    "Plan",
    "ReviewCandidate",
    "ReviewEvidence",
    "ReviewTournamentEvidence",
    "SMEEvidence",
    "Task",
    "TaskStatus",
    "TestEvidence",
    "TournamentEvidence",
]
