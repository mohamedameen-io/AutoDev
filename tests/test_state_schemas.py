"""Tests for :mod:`src.state.schemas` — focused on the v0.6.1 ``Task.requires``
schema field and its legacy-plan migration semantics.

The wider Plan/Phase/Task round-trip is covered by other tests
(``test_state_plan_manager.py``, ``test_orchestrator_plan_phase.py``); this
module tightens coverage on the new field's defaults and validation.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from state.schemas import Task


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
