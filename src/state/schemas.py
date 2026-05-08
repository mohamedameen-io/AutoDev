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

from pydantic import BaseModel, ConfigDict, Field


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


class AcceptanceCriterion(BaseModel):
    """A single check-box acceptance criterion attached to a task."""

    model_config = ConfigDict(extra="forbid")

    id: str
    description: str
    met: bool = False


class Task(BaseModel):
    """Leaf unit of work that the ``coder`` role implements."""

    model_config = ConfigDict(extra="forbid")

    id: str  # "1.1", "1.2", "2.1" — phase.sequence
    phase_id: str  # "1", "2", "3"
    title: str
    description: str
    status: TaskStatus = "pending"
    files: list[str] = Field(default_factory=list)
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
