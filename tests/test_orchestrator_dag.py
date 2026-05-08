"""Tests for :mod:`orchestrator.dag` — DAG validation and scheduling helpers."""

from __future__ import annotations

import pytest

from orchestrator.dag import DagValidationError, validate_phase_dag
from state.schemas import Phase, Task


def _t(tid: str, deps: list[str] | None = None, files: list[str] | None = None) -> Task:
    return Task(
        id=tid,
        phase_id="1",
        title=f"task {tid}",
        description=f"do {tid}",
        depends_on=list(deps or []),
        files=list(files or []),
    )


def _phase(tasks: list[Task]) -> Phase:
    return Phase(id="1", title="Test phase", tasks=tasks)


# ---------------------------------------------------------------------------
# validate_phase_dag
# ---------------------------------------------------------------------------


def test_validate_dag_accepts_valid_chain() -> None:
    """A → B → C with no other deps validates without raising."""
    phase = _phase(
        [_t("1.1"), _t("1.2", ["1.1"]), _t("1.3", ["1.2"])]
    )
    validate_phase_dag(phase)


def test_validate_dag_accepts_empty_phase() -> None:
    """An empty phase is trivially valid."""
    validate_phase_dag(_phase([]))


def test_validate_dag_accepts_diamond() -> None:
    """Fork + merge (diamond) is valid."""
    phase = _phase(
        [
            _t("1.1"),
            _t("1.2", ["1.1"]),
            _t("1.3", ["1.1"]),
            _t("1.4", ["1.2", "1.3"]),
        ]
    )
    validate_phase_dag(phase)


def test_validate_dag_rejects_undefined_dep() -> None:
    """Reference to a task id that doesn't exist in the phase."""
    phase = _phase([_t("1.1"), _t("1.2", ["1.999"])])
    with pytest.raises(DagValidationError, match=r"undefined task '1.999'"):
        validate_phase_dag(phase)


def test_validate_dag_rejects_cycle_with_path_in_error() -> None:
    """A cycle's full path appears in the error message."""
    phase = _phase(
        [
            _t("1.1", ["1.3"]),
            _t("1.2", ["1.1"]),
            _t("1.3", ["1.2"]),
        ]
    )
    with pytest.raises(DagValidationError, match=r"cycle detected"):
        validate_phase_dag(phase)


def test_validate_dag_rejects_self_loop() -> None:
    """A task that depends on itself is a cycle."""
    phase = _phase([_t("1.1", ["1.1"])])
    with pytest.raises(DagValidationError, match=r"cycle detected"):
        validate_phase_dag(phase)


def test_validate_dag_cycle_message_includes_full_path() -> None:
    """The error message names every node in the cycle in order."""
    phase = _phase(
        [
            _t("1.1", ["1.3"]),
            _t("1.2", ["1.1"]),
            _t("1.3", ["1.2"]),
        ]
    )
    try:
        validate_phase_dag(phase)
    except DagValidationError as exc:
        msg = str(exc)
        assert "1.1" in msg
        assert "1.2" in msg
        assert "1.3" in msg
    else:
        raise AssertionError("expected DagValidationError")
