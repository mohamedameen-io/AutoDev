"""Tests for :mod:`src.state.schemas` — focused on the v0.6.1 ``Task.requires``
schema field and its legacy-plan migration semantics.

The wider Plan/Phase/Task round-trip is covered by other tests
(``test_state_plan_manager.py``, ``test_orchestrator_plan_phase.py``); this
module tightens coverage on the new field's defaults and validation.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from state.schemas import AcceptanceCriterion, Phase, Task, TournamentEvidence


# ---------------------------------------------------------------------------
# Legacy migration — the critical compatibility test
# ---------------------------------------------------------------------------


def test_task_load_legacy_plan_without_requires_field() -> None:
    """A v0.6.0-shape Task dict (no ``requires`` key) must parse cleanly with
    ``requires == []``.

    This is the migration guarantee: an existing ``plan.json`` written before
    v0.6.1 (or a hand-edited dict that omits the field) must round-trip
    through ``Task.model_validate`` without error and surface a default
    empty list — not raise, not produce ``None``, not silently corrupt.
    """
    legacy_payload = {
        "id": "1.1",
        "phase_id": "1",
        "title": "Add subtract",
        "description": "Implement subtract(a, b)",
        "status": "pending",
    }
    task = Task.model_validate(legacy_payload)
    assert task.requires == []


def test_task_default_requires_is_empty_list() -> None:
    """Constructing a Task without ``requires`` defaults to an empty list."""
    task = Task(
        id="1.1",
        phase_id="1",
        title="t",
        description="d",
    )
    assert task.requires == []
    assert isinstance(task.requires, list)


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------


def test_task_requires_accepts_known_tokens() -> None:
    """All four documented tokens are accepted and round-trip cleanly."""
    task = Task(
        id="1.1",
        phase_id="1",
        title="t",
        description="d",
        requires=["hardware", "human", "external_service", "manual"],
    )
    assert task.requires == ["hardware", "human", "external_service", "manual"]


def test_task_requires_accepts_subset_of_tokens() -> None:
    """A non-empty subset (e.g. just ``hardware``) is also valid."""
    task = Task(
        id="1.1",
        phase_id="1",
        title="t",
        description="d",
        requires=["hardware"],
    )
    assert task.requires == ["hardware"]


def test_task_requires_validates_tokens() -> None:
    """Passing an unknown token must raise pydantic ``ValidationError``.

    The field is typed ``list[Literal[...]]``, so pydantic enforces the
    Literal constraint — bogus tokens are rejected at construction time.
    Defense in depth: parser drops unknowns before reaching the model,
    but if a hand-crafted plan.json or programmatic caller bypasses the
    parser, the model itself still refuses bad input.
    """
    with pytest.raises(ValidationError):
        Task(
            id="1.1",
            phase_id="1",
            title="t",
            description="d",
            requires=["bogus"],
        )


def test_task_requires_rejects_mixed_valid_and_invalid_tokens() -> None:
    """Even one bad token in an otherwise-valid list rejects the whole field."""
    with pytest.raises(ValidationError):
        Task(
            id="1.1",
            phase_id="1",
            title="t",
            description="d",
            requires=["hardware", "definitely_not_a_real_token"],
        )


def test_task_requires_round_trip_via_model_dump() -> None:
    """``model_dump`` then ``model_validate`` must preserve ``requires``."""
    task = Task(
        id="1.1",
        phase_id="1",
        title="t",
        description="d",
        requires=["hardware", "human"],
    )
    dumped = task.model_dump(mode="json")
    assert dumped["requires"] == ["hardware", "human"]
    restored = Task.model_validate(dumped)
    assert restored.requires == ["hardware", "human"]


# ---------------------------------------------------------------------------
# v0.8.0 — ``Task.complexity`` field
# ---------------------------------------------------------------------------


def test_task_complexity_field_simple() -> None:
    """The ``simple`` Literal is accepted and round-trips."""
    task = Task(
        id="1.1",
        phase_id="1",
        title="t",
        description="d",
        complexity="simple",
    )
    assert task.complexity == "simple"


def test_task_complexity_field_medium() -> None:
    """The ``medium`` Literal is accepted and round-trips."""
    task = Task(
        id="1.1",
        phase_id="1",
        title="t",
        description="d",
        complexity="medium",
    )
    assert task.complexity == "medium"


def test_task_complexity_field_complex() -> None:
    """The ``complex`` Literal is accepted and round-trips."""
    task = Task(
        id="1.1",
        phase_id="1",
        title="t",
        description="d",
        complexity="complex",
    )
    assert task.complexity == "complex"


def test_task_complexity_default_none() -> None:
    """Constructing a Task without ``complexity`` defaults to ``None`` —
    the legacy-plan migration semantics for plans that don't carry the
    architect-emitted per-task directive.
    """
    task = Task(
        id="1.1",
        phase_id="1",
        title="t",
        description="d",
    )
    assert task.complexity is None


def test_task_complexity_invalid_token_raises() -> None:
    """Any token outside the three Literal values raises ValidationError —
    schema-level guard against architect typos like ``"trivial"`` slipping
    into the plan dict.
    """
    with pytest.raises(ValidationError):
        Task(
            id="1.1",
            phase_id="1",
            title="t",
            description="d",
            complexity="trivial",  # type: ignore[arg-type]
        )


def test_task_load_legacy_without_complexity_returns_none() -> None:
    """A v0.7.0-shape Task dict (no ``complexity`` key) must parse with
    ``complexity == None`` — the on-disk migration guarantee: existing
    plan.json files written by v0.7.0 keep loading after v0.8.0.
    """
    legacy_payload = {
        "id": "1.1",
        "phase_id": "1",
        "title": "Add subtract",
        "description": "Implement subtract(a, b)",
        "status": "pending",
    }
    task = Task.model_validate(legacy_payload)
    assert task.complexity is None


# ---------------------------------------------------------------------------
# v0.9.0 — ``Phase`` extension fields
# ---------------------------------------------------------------------------


def _make_dummy_task(id: str = "1.1") -> Task:
    return Task(id=id, phase_id="1", title="t", description="d")


def test_phase_acceptance_field() -> None:
    """``Phase.acceptance`` accepts a list of ``AcceptanceCriterion``."""
    crit = AcceptanceCriterion(id="ac-1", description="all tests pass")
    phase = Phase(
        id="1",
        title="Setup",
        tasks=[_make_dummy_task()],
        acceptance=[crit],
    )
    assert len(phase.acceptance) == 1
    assert phase.acceptance[0].description == "all tests pass"


def test_phase_baseline_commit_default_none() -> None:
    """``Phase.baseline_commit`` defaults to ``None`` until execute_phase
    captures it at phase entry."""
    phase = Phase(id="1", title="Setup", tasks=[_make_dummy_task()])
    assert phase.baseline_commit is None


def test_phase_review_status_states() -> None:
    """All literal values are accepted on ``Phase.review_status``.

    The state machine: ``None`` → ``"in_progress"`` → terminal
    (``"accepted"`` | ``"corrective_required"`` | ``"skipped"``). The
    ``"pending"`` value is reserved for future use. v0.29.0 Bug 7 adds
    the non-terminal ``"paused"`` value, set by the phase aggregator
    when a quarantined task halts the phase.
    """
    for status in (
        "pending",
        "in_progress",
        "accepted",
        "corrective_required",
        "skipped",
        "paused",
    ):
        phase = Phase(
            id="1",
            title="Setup",
            tasks=[_make_dummy_task()],
            review_status=status,  # type: ignore[arg-type]
        )
        assert phase.review_status == status


def test_phase_corrective_task_ids_default_empty() -> None:
    """``Phase.corrective_task_ids`` defaults to ``[]``; new injections
    append to it."""
    phase = Phase(id="1", title="Setup", tasks=[_make_dummy_task()])
    assert phase.corrective_task_ids == []
    phase.corrective_task_ids.append("1.c1")
    assert phase.corrective_task_ids == ["1.c1"]


def test_legacy_phase_loads_with_defaults() -> None:
    """A v0.8.0-shape Phase dict (no acceptance/baseline_commit/
    review_status/corrective_task_ids) loads with all four new fields at
    defaults — the migration guarantee."""
    legacy_payload = {
        "id": "1",
        "title": "Setup",
        "description": "",
        "tasks": [
            {
                "id": "1.1",
                "phase_id": "1",
                "title": "t",
                "description": "d",
            }
        ],
    }
    phase = Phase.model_validate(legacy_payload)
    assert phase.acceptance == []
    assert phase.baseline_commit is None
    assert phase.review_status is None
    assert phase.corrective_task_ids == []


# ---------------------------------------------------------------------------
# v0.9.0 — ``TournamentEvidence.phase`` extension
# ---------------------------------------------------------------------------


def test_tournament_evidence_phase_review_kind() -> None:
    """``TournamentEvidence.phase = "phase_review"`` is now a valid value."""
    ev = TournamentEvidence(
        task_id="phase-1",
        tournament_id="phase-review-abcd1234-1",
        phase="phase_review",
        passes=2,
        winner="A",
        converged=True,
    )
    assert ev.phase == "phase_review"


# ---------------------------------------------------------------------------
# v0.14.0 — ``Plan.edit_scope`` and ``Phase.edit_scope`` fields with validators
# ---------------------------------------------------------------------------


def _make_dummy_plan_kwargs(
    edit_scope: list[str] | None = None,
    phases: list[Phase] | None = None,
) -> dict:
    from state.schemas import Plan as _PlanType  # noqa: F401

    out: dict = {
        "plan_id": "plan-test",
        "spec_hash": "abcdef0123456789",
        "phases": phases if phases is not None else [
            Phase(id="1", title="Setup", tasks=[_make_dummy_task()])
        ],
        "created_at": "2025-01-01T00:00:00+00:00",
        "updated_at": "2025-01-01T00:00:00+00:00",
    }
    if edit_scope is not None:
        out["edit_scope"] = edit_scope
    return out


def test_plan_edit_scope_default_empty() -> None:
    """``Plan.edit_scope`` defaults to an empty list — legacy whole-repo
    semantics. No constraint enforced when empty."""
    from state.schemas import Plan

    plan = Plan(**_make_dummy_plan_kwargs())
    assert plan.edit_scope == []


def test_plan_edit_scope_rejects_absolute_path() -> None:
    """Absolute paths are rejected at construction — scope is repo-relative."""
    from pydantic import ValidationError as _ValidationError

    from state.schemas import Plan

    with pytest.raises(_ValidationError):
        Plan(**_make_dummy_plan_kwargs(edit_scope=["/etc/passwd"]))


def test_plan_edit_scope_rejects_dotdot() -> None:
    """Paths containing ``..`` segments are rejected — would break
    is_in_scope semantics by escaping the repo root."""
    from pydantic import ValidationError as _ValidationError

    from state.schemas import Plan

    with pytest.raises(_ValidationError):
        Plan(**_make_dummy_plan_kwargs(edit_scope=["../outside"]))
    with pytest.raises(_ValidationError):
        Plan(**_make_dummy_plan_kwargs(edit_scope=["src/../etc"]))


def test_plan_edit_scope_strips_trailing_slash() -> None:
    """Trailing slashes are normalized away so ``"src/"`` and ``"src"``
    are equivalent for prefix matching downstream."""
    from state.schemas import Plan

    plan = Plan(**_make_dummy_plan_kwargs(edit_scope=["src/", "tests/"]))
    assert plan.edit_scope == ["src", "tests"]


def test_phase_edit_scope_optional_inherits_plan() -> None:
    """``Phase.edit_scope`` defaults to ``None`` — inherit semantics, not
    "empty" semantics. Distinct from Plan.edit_scope which defaults to []."""
    phase = Phase(id="1", title="Setup", tasks=[_make_dummy_task()])
    assert phase.edit_scope is None


def test_phase_edit_scope_validators_match_plan() -> None:
    """Phase-level scope override is validated identically to Plan.edit_scope."""
    from pydantic import ValidationError as _ValidationError

    # Trailing slash stripped
    phase = Phase(
        id="1",
        title="Setup",
        tasks=[_make_dummy_task()],
        edit_scope=["src/foo/"],
    )
    assert phase.edit_scope == ["src/foo"]

    # Absolute path rejected
    with pytest.raises(_ValidationError):
        Phase(
            id="1",
            title="Setup",
            tasks=[_make_dummy_task()],
            edit_scope=["/abs"],
        )

    # ``..`` rejected
    with pytest.raises(_ValidationError):
        Phase(
            id="1",
            title="Setup",
            tasks=[_make_dummy_task()],
            edit_scope=["src/../etc"],
        )
