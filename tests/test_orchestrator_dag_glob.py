"""v0.17.0 S5: glob-aware ``find_file_overlaps`` + ``validate_edit_scope``.

The architect can declare ``Task.files = ["src/qa/*.py"]`` to claim a
glob pattern instead of enumerating every file. The DAG scheduler must
expand globs against a project-wide tracked-files cache before computing
intersections; otherwise two tasks with overlapping globs would race on
apply.

Tests cover three scenarios:

1. Two tasks declaring overlapping globs detect the overlap on every
   tracked file in the intersection.
2. A task with an explicit path overlaps with a task declaring a glob
   that matches the path.
3. ``validate_edit_scope`` accepts a glob entry whose expanded files all
   lie under the resolved scope, and rejects when even one expansion
   escapes the scope.
"""

from __future__ import annotations

import pytest

from orchestrator.dag import (
    EditScopeViolation,
    find_file_overlaps,
    validate_edit_scope,
)
from state.schemas import Phase, Plan, Task


def _t(tid: str, files: list[str]) -> Task:
    return Task(
        id=tid,
        phase_id="1",
        title=f"task {tid}",
        description=f"do {tid}",
        files=files,
    )


# ---------------------------------------------------------------------------
# find_file_overlaps with tracked_files (glob expansion)
# ---------------------------------------------------------------------------


def test_overlap_with_glob_vs_glob() -> None:
    """Two tasks both declaring ``src/qa/*.py``-style globs overlap."""
    tasks = [
        _t("a", ["src/qa/*.py"]),
        _t("b", ["src/qa/*.py"]),
    ]
    tracked = {"src/qa/foo.py", "src/qa/bar.py", "src/other/x.py"}
    overlaps = find_file_overlaps(tasks, tracked_files=tracked)
    assert overlaps["a"] == {"b"}
    assert overlaps["b"] == {"a"}


def test_overlap_with_glob_vs_explicit() -> None:
    """A glob task overlaps with an explicit-file task that matches it."""
    tasks = [
        _t("a", ["src/qa/*.py"]),
        _t("b", ["src/qa/foo.py"]),
        _t("c", ["src/other/x.py"]),
    ]
    tracked = {"src/qa/foo.py", "src/qa/bar.py", "src/other/x.py"}
    overlaps = find_file_overlaps(tasks, tracked_files=tracked)
    assert overlaps["a"] == {"b"}
    assert overlaps["b"] == {"a"}
    assert overlaps["c"] == set()


def test_no_overlap_when_globs_disjoint() -> None:
    """Globs that match different sets of tracked files don't overlap."""
    tasks = [
        _t("a", ["src/qa/*.py"]),
        _t("b", ["src/other/*.py"]),
    ]
    tracked = {"src/qa/foo.py", "src/other/x.py"}
    overlaps = find_file_overlaps(tasks, tracked_files=tracked)
    assert overlaps["a"] == set()
    assert overlaps["b"] == set()


def test_legacy_explicit_overlap_unchanged_when_no_tracked_files() -> None:
    """Without ``tracked_files``, behavior matches the legacy path."""
    tasks = [
        _t("a", ["src/qa/foo.py"]),
        _t("b", ["src/qa/foo.py"]),
        _t("c", ["src/qa/bar.py"]),
    ]
    overlaps = find_file_overlaps(tasks)
    assert overlaps["a"] == {"b"}
    assert overlaps["b"] == {"a"}
    assert overlaps["c"] == set()


def test_glob_with_no_tracked_cache_falls_back_to_literal() -> None:
    """Without a tracked-files cache, glob-bearing entries are matched literally.

    This preserves backward compatibility: legacy callers that don't
    plumb a cache through still get deterministic (if conservative)
    behavior.
    """
    tasks = [
        _t("a", ["src/qa/*.py"]),
        _t("b", ["src/qa/*.py"]),
    ]
    # No tracked_files: literal string equality on the glob entries.
    overlaps = find_file_overlaps(tasks)
    assert overlaps["a"] == {"b"}
    assert overlaps["b"] == {"a"}


# ---------------------------------------------------------------------------
# validate_edit_scope with glob expansion
# ---------------------------------------------------------------------------


def _phase_with_task(task: Task, edit_scope: list[str] | None = None) -> Phase:
    return Phase(id="1", title="P", tasks=[task], edit_scope=edit_scope)


def _plan(phase: Phase, edit_scope: list[str] | None = None) -> Plan:
    return Plan(
        plan_id="p1",
        spec_hash="h",
        complexity="simple",
        edit_scope=edit_scope or [],
        phases=[phase],
        created_at="2026-05-08T00:00:00Z",
        updated_at="2026-05-08T00:00:00Z",
    )


def test_validate_edit_scope_accepts_glob_in_scope() -> None:
    """A glob whose expanded files all lie under scope is accepted."""
    task = _t("a", ["src/qa/*.py"])
    phase = _phase_with_task(task, edit_scope=["src"])
    plan = _plan(phase)
    tracked = {"src/qa/foo.py", "src/qa/bar.py"}
    # Should NOT raise.
    validate_edit_scope(plan, tracked_files=tracked)


def test_validate_edit_scope_rejects_glob_outside_scope() -> None:
    """A glob whose expanded files escape scope raises EditScopeViolation."""
    task = _t("a", ["**/*.py"])
    phase = _phase_with_task(task, edit_scope=["src"])
    plan = _plan(phase)
    tracked = {"src/qa/foo.py", "scripts/util.py"}
    with pytest.raises(EditScopeViolation):
        validate_edit_scope(plan, tracked_files=tracked)


def test_validate_edit_scope_glob_with_zero_matches_is_noop() -> None:
    """A glob matching no tracked files is treated as empty (no violation)."""
    task = _t("a", ["src/nonexistent/*.py"])
    phase = _phase_with_task(task, edit_scope=["src"])
    plan = _plan(phase)
    tracked = {"src/qa/foo.py"}  # no match for src/nonexistent/*
    # Treated as empty expansion — no violation, but should not crash.
    validate_edit_scope(plan, tracked_files=tracked)


def test_validate_edit_scope_explicit_path_unchanged() -> None:
    """Explicit (non-glob) paths still validated literally."""
    task = _t("a", ["src/qa/foo.py"])
    phase = _phase_with_task(task, edit_scope=["src"])
    plan = _plan(phase)
    # No tracked_files needed — explicit paths bypass expansion.
    validate_edit_scope(plan)
