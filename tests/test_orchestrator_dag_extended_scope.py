"""v0.20.0 C1: validate_edit_scope honors Task.extended_scope."""

from __future__ import annotations

import pytest

from orchestrator.dag import EditScopeViolation, validate_edit_scope
from state.schemas import Phase, Plan, Task


def _plan(*tasks: Task, edit_scope: list[str] | None = None) -> Plan:
    return Plan(
        plan_id="p-1",
        spec_hash="abc",
        phases=[
            Phase(id="1", title="P1", description="", tasks=list(tasks))
        ],
        complexity="simple",
        created_at="2024-01-01T00:00:00+00:00",
        updated_at="2024-01-01T00:00:00+00:00",
        edit_scope=edit_scope or [],
    )


def test_extended_scope_admits_paths_outside_plan_scope() -> None:
    """A task with ``extended_scope`` may declare files outside ``plan.edit_scope``."""
    t = Task(
        id="1.1",
        phase_id="1",
        title="t",
        description="d",
        files=["src/qa/special.py"],
        extended_scope=["src/qa"],
    )
    plan = _plan(t, edit_scope=["src/orchestrator"])
    # Should not raise — extended_scope covers src/qa
    validate_edit_scope(plan)


def test_no_extended_scope_violates_when_outside_plan_scope() -> None:
    """Without extended_scope, a file outside the plan scope still raises."""
    t = Task(
        id="1.1",
        phase_id="1",
        title="t",
        description="d",
        files=["src/qa/special.py"],
    )
    plan = _plan(t, edit_scope=["src/orchestrator"])
    with pytest.raises(EditScopeViolation):
        validate_edit_scope(plan)


def test_extended_scope_does_not_save_truly_outside_paths() -> None:
    """A file outside BOTH plan.edit_scope AND extended_scope still raises."""
    t = Task(
        id="1.1",
        phase_id="1",
        title="t",
        description="d",
        files=["leaks/secret.txt"],
        extended_scope=["src/qa"],
    )
    plan = _plan(t, edit_scope=["src/orchestrator"])
    with pytest.raises(EditScopeViolation):
        validate_edit_scope(plan)


def test_default_empty_extended_scope_does_not_break_legacy() -> None:
    """Empty extended_scope (default) preserves byte-identical v0.19.0 behavior."""
    t = Task(
        id="1.1",
        phase_id="1",
        title="t",
        description="d",
        files=["src/orchestrator/x.py"],
    )
    plan = _plan(t, edit_scope=["src/orchestrator"])
    # Should not raise
    validate_edit_scope(plan)


def test_extended_scope_works_with_globs() -> None:
    """Glob expansion respects extended_scope as a fallback."""
    t = Task(
        id="1.1",
        phase_id="1",
        title="t",
        description="d",
        files=["src/qa/*.py"],
        extended_scope=["src/qa"],
    )
    plan = _plan(t, edit_scope=["src/orchestrator"])
    tracked = {"src/qa/special.py", "src/qa/utils.py"}
    # Should not raise
    validate_edit_scope(plan, tracked_files=tracked)


def test_empty_resolved_scope_skips_check_even_with_extended() -> None:
    """When resolved scope is empty (whole-repo), extended_scope is harmless."""
    t = Task(
        id="1.1",
        phase_id="1",
        title="t",
        description="d",
        files=["any/where.txt"],
        extended_scope=["src/qa"],
    )
    plan = _plan(t, edit_scope=[])
    # Empty plan_scope + empty phase scope → whole repo allowed
    validate_edit_scope(plan)
