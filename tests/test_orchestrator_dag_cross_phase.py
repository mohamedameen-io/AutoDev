"""Tests for the plan-wide (cross-phase) DAG validators in :mod:`orchestrator.dag`.

C3: the per-phase :func:`validate_phase_dag` scopes its undefined-ref check to a
single phase's task ids, so a legitimate cross-phase dep (task ``2.1`` depending
on ``1.1``) is wrongly flagged as "undefined". These tests pin the new plan-wide
validators that Phase 3 will call instead:

* :func:`validate_dag_undefined_refs` — plan-wide undefined-ref check.
* :func:`validate_dag_cycles_global` — plan-wide cycle detection.
"""

from __future__ import annotations

import pytest

from orchestrator.dag import (
    DagValidationError,
    validate_dag_cycles_global,
    validate_dag_undefined_refs,
)
from state.schemas import Phase, Plan, Task


def _t(
    tid: str,
    phase_id: str,
    deps: list[str] | None = None,
    files: list[str] | None = None,
) -> Task:
    return Task(
        id=tid,
        phase_id=phase_id,
        title=f"task {tid}",
        description=f"do {tid}",
        depends_on=list(deps or []),
        files=list(files or []),
    )


def _phase(pid: str, tasks: list[Task]) -> Phase:
    return Phase(id=pid, title=f"Phase {pid}", tasks=tasks)


def _plan(phases: list[Phase]) -> Plan:
    return Plan(
        plan_id="plan-cross-phase",
        spec_hash="abcdef0123456789",
        phases=phases,
        edit_scope=[],
        created_at="2025-01-01T00:00:00+00:00",
        updated_at="2025-01-01T00:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# validate_dag_undefined_refs — plan-wide undefined-ref check
# ---------------------------------------------------------------------------


def test_undefined_refs_accepts_cross_phase_dep() -> None:
    """Task 2.1 depends_on 1.1 (a different phase) must NOT be 'undefined'.

    This is the exact false-positive C3 fixes: the per-phase validator only
    sees phase 2's ids, so 1.1 looks undefined. The plan-wide validator builds
    an index over every phase and accepts it.
    """
    plan = _plan(
        [
            _phase("1", [_t("1.1", "1")]),
            _phase("2", [_t("2.1", "2", deps=["1.1"])]),
        ]
    )
    validate_dag_undefined_refs(plan)  # must not raise


def test_undefined_refs_accepts_within_phase_dep() -> None:
    """A same-phase dep (the legacy common case) still validates."""
    plan = _plan(
        [
            _phase("1", [_t("1.1", "1"), _t("1.2", "1", deps=["1.1"])]),
        ]
    )
    validate_dag_undefined_refs(plan)


def test_undefined_refs_accepts_empty_plan() -> None:
    """A plan with no phases / no tasks is trivially valid."""
    validate_dag_undefined_refs(_plan([]))
    validate_dag_undefined_refs(_plan([_phase("1", [])]))


def test_undefined_refs_raises_on_truly_missing_id() -> None:
    """A dep on an id that exists in NO phase raises DagValidationError."""
    plan = _plan(
        [
            _phase("1", [_t("1.1", "1")]),
            _phase("2", [_t("2.1", "2", deps=["9.9"])]),
        ]
    )
    with pytest.raises(DagValidationError, match=r"undefined task '9.9'"):
        validate_dag_undefined_refs(plan)


def test_undefined_refs_error_names_offending_task() -> None:
    """The error message names both the offending task and the missing dep."""
    plan = _plan(
        [
            _phase("1", [_t("1.1", "1")]),
            _phase("2", [_t("2.1", "2", deps=["nope"])]),
        ]
    )
    try:
        validate_dag_undefined_refs(plan)
    except DagValidationError as exc:
        msg = str(exc)
        assert "2.1" in msg
        assert "nope" in msg
    else:
        raise AssertionError("expected DagValidationError")


# ---------------------------------------------------------------------------
# validate_dag_cycles_global — plan-wide cycle detection
# ---------------------------------------------------------------------------


def test_cycles_global_accepts_acyclic_cross_phase_dag() -> None:
    """A forward-only cross-phase DAG has no cycle."""
    plan = _plan(
        [
            _phase("1", [_t("1.1", "1"), _t("1.2", "1", deps=["1.1"])]),
            _phase("2", [_t("2.1", "2", deps=["1.2"])]),
        ]
    )
    validate_dag_cycles_global(plan)  # must not raise


def test_cycles_global_accepts_empty_plan() -> None:
    """No tasks → no cycles."""
    validate_dag_cycles_global(_plan([]))


def test_cycles_global_raises_on_cross_phase_cycle() -> None:
    """A back-edge from phase 2 to phase 1 closing a loop is a cycle.

    1.1 -> 2.1 -> 1.1 (1.1 depends_on 2.1, 2.1 depends_on 1.1).
    """
    plan = _plan(
        [
            _phase("1", [_t("1.1", "1", deps=["2.1"])]),
            _phase("2", [_t("2.1", "2", deps=["1.1"])]),
        ]
    )
    with pytest.raises(DagValidationError, match=r"cycle detected"):
        validate_dag_cycles_global(plan)


def test_cycles_global_cycle_message_names_nodes() -> None:
    """The cycle error path includes the looping task ids."""
    plan = _plan(
        [
            _phase("1", [_t("1.1", "1", deps=["2.1"])]),
            _phase("2", [_t("2.1", "2", deps=["1.1"])]),
        ]
    )
    try:
        validate_dag_cycles_global(plan)
    except DagValidationError as exc:
        msg = str(exc)
        assert "1.1" in msg
        assert "2.1" in msg
    else:
        raise AssertionError("expected DagValidationError")
