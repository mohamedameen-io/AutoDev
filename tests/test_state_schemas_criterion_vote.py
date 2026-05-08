"""v0.18.0 C2: tests for CriterionVote + AcceptanceCriterion.vote_history."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from state.schemas import AcceptanceCriterion, CriterionVote


def test_criterion_vote_basic() -> None:
    cv = CriterionVote(
        judge_role="critic",
        verdict="APPROVE",
        justification="looks good",
        timestamp="2026-01-01T00:00:00Z",
    )
    assert cv.judge_role == "critic"
    assert cv.verdict == "APPROVE"
    assert cv.justification == "looks good"


def test_criterion_vote_defaults() -> None:
    cv = CriterionVote(judge_role="reviewer", verdict="REJECT")
    assert cv.justification == ""
    assert cv.timestamp == ""


def test_criterion_vote_invalid_verdict() -> None:
    with pytest.raises(ValidationError):
        CriterionVote(judge_role="x", verdict="WAFFLE")  # type: ignore[arg-type]


def test_acceptance_criterion_vote_history_defaults_empty() -> None:
    ac = AcceptanceCriterion(id="ac1", description="x")
    assert ac.vote_history == []


def test_acceptance_criterion_vote_history_persisted() -> None:
    cv = CriterionVote(judge_role="critic", verdict="APPROVE")
    ac = AcceptanceCriterion(id="ac1", description="x", vote_history=[cv])
    assert ac.vote_history == [cv]
    # Pydantic round-trip.
    dump = ac.model_dump()
    parsed = AcceptanceCriterion.model_validate(dump)
    assert parsed.vote_history[0].verdict == "APPROVE"


def test_acceptance_criterion_extra_forbid() -> None:
    """Both AcceptanceCriterion and CriterionVote enforce extra='forbid'."""
    with pytest.raises(ValidationError):
        AcceptanceCriterion(id="ac1", description="x", bogus_field="y")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        CriterionVote(judge_role="x", verdict="APPROVE", bogus="y")  # type: ignore[call-arg]
