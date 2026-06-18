"""v0.18.0 C3: tests for JudgeRecusal."""

from __future__ import annotations


from orchestrator.recusal import JudgeRecusal
from state.schemas import Task


def test_no_prior_evidence_no_recusal() -> None:
    rc = JudgeRecusal()
    task = Task(id="1.1", phase_id="1", title="t", description="t")
    assert rc.should_recuse("critic", task) is False


def test_empty_evidence_no_recusal() -> None:
    rc = JudgeRecusal()
    task = Task(id="1.1", phase_id="1", title="t", description="t")
    assert rc.should_recuse("critic", task, prior_evidence=[]) is False


def test_evidence_role_match_recusal() -> None:
    """A prior evidence entry with role=critic causes the critic judge to recuse."""
    rc = JudgeRecusal()
    task = Task(id="1.1", phase_id="1", title="t", description="t")
    evidence = [
        {"role": "developer", "summary": "implemented x"},
        {"role": "critic", "summary": "reviewed prior version"},
    ]
    assert rc.should_recuse("critic", task, prior_evidence=evidence) is True


def test_evidence_no_role_match_no_recusal() -> None:
    rc = JudgeRecusal()
    task = Task(id="1.1", phase_id="1", title="t", description="t")
    evidence = [
        {"role": "developer", "summary": "x"},
        {"role": "test_engineer", "summary": "y"},
    ]
    assert rc.should_recuse("reviewer", task, prior_evidence=evidence) is False


def test_assigned_agent_match_recusal() -> None:
    """Task.assigned_agent matching judge_role triggers recusal."""
    rc = JudgeRecusal()
    task = Task(
        id="1.1", phase_id="1", title="t", description="t",
        assigned_agent="developer",
    )
    assert rc.should_recuse("developer", task) is True


def test_alternate_role_field_name() -> None:
    """Recusal detects evidence with 'agent_role' field instead of 'role'."""
    rc = JudgeRecusal()
    task = Task(id="1.1", phase_id="1", title="t", description="t")
    evidence = [{"agent_role": "test_engineer", "summary": "x"}]
    assert rc.should_recuse("test_engineer", task, prior_evidence=evidence) is True


def test_non_dict_entries_ignored() -> None:
    """Non-dict entries in prior_evidence don't crash the detector."""
    rc = JudgeRecusal()
    task = Task(id="1.1", phase_id="1", title="t", description="t")
    evidence = ["string-value", 42, None, {"role": "critic"}]
    assert rc.should_recuse("critic", task, prior_evidence=evidence) is True  # type: ignore[arg-type]
    assert rc.should_recuse("reviewer", task, prior_evidence=evidence) is False  # type: ignore[arg-type]
