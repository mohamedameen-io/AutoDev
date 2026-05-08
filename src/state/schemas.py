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

    @field_validator("files", mode="after")
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
    escalated: bool = False
    assigned_agent: str | None = None  # usually "developer"
    evidence_bundle: str | None = None  # path (relative to repo root) to evidence json
    blocked_reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


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
    # v0.9.0: phase-review state machine. ``None`` (initial) →
    # ``"in_progress"`` (when the tournament starts) → ``"accepted"`` |
    # ``"corrective_required"`` | ``"skipped"`` (terminal). The orchestrator
    # uses this as a critical loop guard: once a phase has been reviewed,
    # the next observation of all-terminal task state does NOT re-fire the
    # tournament. Corrective tasks landing terminal transition the status
    # from ``"corrective_required"`` → ``"accepted"`` directly.
    review_status: (
        Literal["pending", "in_progress", "accepted", "corrective_required", "skipped"]
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


class ReviewEvidence(_BaseEvidence):
    """Artifact produced by the ``reviewer`` role."""

    kind: Literal["review"] = "review"
    verdict: Literal["APPROVED", "NEEDS_CHANGES", "REJECTED"]
    issues: list[str] = Field(default_factory=list)
    output_text: str = ""


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


Evidence = Annotated[
    Union[
        CoderEvidence,
        ReviewEvidence,
        TestEvidence,
        ExploreEvidence,
        SMEEvidence,
        CriticEvidence,
        TournamentEvidence,
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
    "ReviewEvidence",
    "SMEEvidence",
    "Task",
    "TaskStatus",
    "TestEvidence",
    "TournamentEvidence",
]
