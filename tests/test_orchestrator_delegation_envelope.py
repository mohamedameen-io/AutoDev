"""Tests for :mod:`src.orchestrator.delegation_envelope`."""

from __future__ import annotations

import pytest

from orchestrator.delegation_envelope import DelegationEnvelope


def test_json_round_trip() -> None:
    env = DelegationEnvelope(
        task_id="1.1",
        target_agent="developer",
        action="implement",
        files=["a.py", "b.py"],
        constraints=["no new deps"],
        acceptance="tests pass",
        context={"model": "sonnet", "retry_count": 1},
    )
    raw = env.model_dump_json()
    back = DelegationEnvelope.model_validate_json(raw)
    assert back == env


def test_render_as_task_message_non_empty_and_mentions_task_id() -> None:
    env = DelegationEnvelope(
        task_id="2.3",
        target_agent="reviewer",
        action="review",
        files=["x.py"],
        acceptance="approve or flag issues",
    )
    msg = env.render_as_task_message()
    assert msg
    assert "2.3" in msg
    assert "reviewer" in msg
    assert "review" in msg
    assert "x.py" in msg
    assert "TASK:" in msg


def test_render_as_task_message_includes_context() -> None:
    env = DelegationEnvelope(
        task_id="3.1",
        target_agent="critic",
        action="critique",
        context={"retry_count": 2, "prior_issues": "xyz"},
    )
    msg = env.render_as_task_message()
    assert "retry_count" in msg
    assert "2" in msg


def test_missing_optional_fields_serializes_cleanly() -> None:
    env = DelegationEnvelope(
        task_id="1.1", target_agent="developer", action="implement"
    )
    msg = env.render_as_task_message()
    # Should still contain the mandatory header lines.
    assert "TASK: 1.1" in msg
    assert "AGENT: developer" in msg
    assert "ACTION: implement" in msg


def test_invalid_action_rejected() -> None:
    with pytest.raises(Exception):
        DelegationEnvelope(task_id="1.1", target_agent="developer", action="bogus")  # type: ignore[arg-type]
