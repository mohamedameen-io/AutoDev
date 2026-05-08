"""Tests for :mod:`orchestrator.dag` — DAG validation and scheduling helpers."""

from __future__ import annotations

import pytest

from orchestrator.dag import (
    DagValidationError,
    EditScopeViolation,
    find_blocked_descendants,
    find_file_overlaps,
    is_in_scope,
    topological_levels,
    validate_edit_scope,
    validate_phase_dag,
)
from state.schemas import Phase, Plan, Task


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


# ---------------------------------------------------------------------------
# topological_levels
# ---------------------------------------------------------------------------


def test_topological_levels_empty_phase() -> None:
    """No tasks → no levels."""
    assert topological_levels(_phase([])) == []


def test_topological_levels_single_task_no_deps_returns_one_level() -> None:
    """Single task → one level containing that task."""
    t = _t("1.1")
    levels = topological_levels(_phase([t]))
    assert len(levels) == 1
    assert [x.id for x in levels[0]] == ["1.1"]


def test_topological_levels_chain_returns_levels_per_task() -> None:
    """A → B → C → 3 levels, one task each."""
    phase = _phase(
        [_t("1.1"), _t("1.2", ["1.1"]), _t("1.3", ["1.2"])]
    )
    levels = topological_levels(phase)
    assert [[x.id for x in lv] for lv in levels] == [
        ["1.1"],
        ["1.2"],
        ["1.3"],
    ]


def test_topological_levels_diamond_correct_grouping() -> None:
    """Diamond: A → {B, C} → D forms 3 levels with B, C in level 1."""
    phase = _phase(
        [
            _t("1.1"),
            _t("1.2", ["1.1"]),
            _t("1.3", ["1.1"]),
            _t("1.4", ["1.2", "1.3"]),
        ]
    )
    levels = topological_levels(phase)
    assert [{x.id for x in lv} for lv in levels] == [
        {"1.1"},
        {"1.2", "1.3"},
        {"1.4"},
    ]


def test_topological_levels_independent_tasks_one_level() -> None:
    """Tasks with no inter-deps all land in level 0."""
    phase = _phase([_t("1.1"), _t("1.2"), _t("1.3")])
    levels = topological_levels(phase)
    assert len(levels) == 1
    assert {x.id for x in levels[0]} == {"1.1", "1.2", "1.3"}


# ---------------------------------------------------------------------------
# find_blocked_descendants
# ---------------------------------------------------------------------------


def test_find_blocked_descendants_walks_reverse_edges() -> None:
    """Failing 1.1 → blocks 1.2 (depends on 1.1) and 1.3 (depends on 1.2)."""
    phase = _phase(
        [_t("1.1"), _t("1.2", ["1.1"]), _t("1.3", ["1.2"])]
    )
    desc = find_blocked_descendants(phase, {"1.1"})
    assert {t.id for t in desc} == {"1.2", "1.3"}


def test_find_blocked_descendants_does_not_include_failed_id() -> None:
    """The failed task id itself is NOT returned as a descendant."""
    phase = _phase([_t("1.1"), _t("1.2", ["1.1"])])
    desc = find_blocked_descendants(phase, {"1.1"})
    assert "1.1" not in {t.id for t in desc}


def test_find_blocked_descendants_diamond_blocks_both_arms() -> None:
    """Fork: failing root blocks both children (and any merged grandchild)."""
    phase = _phase(
        [
            _t("1.1"),
            _t("1.2", ["1.1"]),
            _t("1.3", ["1.1"]),
            _t("1.4", ["1.2", "1.3"]),
        ]
    )
    desc = find_blocked_descendants(phase, {"1.1"})
    assert {t.id for t in desc} == {"1.2", "1.3", "1.4"}


def test_find_blocked_descendants_independent_task_not_blocked() -> None:
    """Failing 1.1 does NOT block a sibling that doesn't depend on 1.1."""
    phase = _phase([_t("1.1"), _t("1.2"), _t("1.3", ["1.1"])])
    desc = find_blocked_descendants(phase, {"1.1"})
    assert {t.id for t in desc} == {"1.3"}


def test_find_blocked_descendants_empty_failed_set_returns_empty() -> None:
    """No failures → no descendants."""
    phase = _phase([_t("1.1"), _t("1.2", ["1.1"])])
    assert find_blocked_descendants(phase, set()) == []


def test_find_blocked_descendants_empty_phase() -> None:
    """No tasks → no descendants regardless of failed set."""
    assert find_blocked_descendants(_phase([]), {"x.y"}) == []


# ---------------------------------------------------------------------------
# find_file_overlaps
# ---------------------------------------------------------------------------


def test_find_file_overlaps_detects_shared_files() -> None:
    """Two tasks sharing a file land in each other's overlap sets."""
    tasks = [
        _t("1.1", files=["src/foo.py"]),
        _t("1.2", files=["src/foo.py", "src/bar.py"]),
    ]
    out = find_file_overlaps(tasks)
    assert out["1.1"] == {"1.2"}
    assert out["1.2"] == {"1.1"}


def test_find_file_overlaps_no_overlap_means_empty_sets() -> None:
    """Disjoint file lists → all overlap sets are empty."""
    tasks = [
        _t("1.1", files=["src/a.py"]),
        _t("1.2", files=["src/b.py"]),
        _t("1.3", files=[]),
    ]
    out = find_file_overlaps(tasks)
    assert out == {"1.1": set(), "1.2": set(), "1.3": set()}


def test_find_file_overlaps_symmetric() -> None:
    """A overlaps B ⟹ B overlaps A — the relation is symmetric."""
    tasks = [
        _t("1.1", files=["x.py"]),
        _t("1.2", files=["x.py"]),
        _t("1.3", files=["x.py"]),
    ]
    out = find_file_overlaps(tasks)
    for a, b in [("1.1", "1.2"), ("1.2", "1.3"), ("1.1", "1.3")]:
        assert b in out[a]
        assert a in out[b]


def test_find_file_overlaps_empty_files_no_overlap() -> None:
    """A task with no files cannot overlap anyone."""
    tasks = [
        _t("1.1", files=[]),
        _t("1.2", files=["x.py"]),
    ]
    out = find_file_overlaps(tasks)
    assert out == {"1.1": set(), "1.2": set()}


def test_find_file_overlaps_includes_all_task_ids_as_keys() -> None:
    """Every task id appears as a key, even with empty overlap set."""
    tasks = [_t("1.1"), _t("1.2", files=["x.py"])]
    out = find_file_overlaps(tasks)
    assert set(out.keys()) == {"1.1", "1.2"}


# ---------------------------------------------------------------------------
# v0.14.0 — validate_edit_scope + is_in_scope
# ---------------------------------------------------------------------------


def _make_plan(
    edit_scope: list[str] | None = None,
    phases: list[Phase] | None = None,
) -> Plan:
    if phases is None:
        phases = [_phase([_t("1.1", files=["src/foo.py"])])]
    return Plan(
        plan_id="plan-test",
        spec_hash="abcdef0123456789",
        phases=phases,
        edit_scope=edit_scope or [],
        created_at="2025-01-01T00:00:00+00:00",
        updated_at="2025-01-01T00:00:00+00:00",
    )


def test_validate_edit_scope_empty_is_noop() -> None:
    """Plan.edit_scope == [] (legacy) → no-op regardless of task files."""
    plan = _make_plan(
        edit_scope=[],
        phases=[_phase([_t("1.1", files=["src/foo.py", "wherever/else.py"])])],
    )
    # Should not raise — empty scope means whole-repo allowed.
    validate_edit_scope(plan)


def test_validate_edit_scope_passes_when_all_files_in_scope() -> None:
    """Every task's files lie under the configured scope → validates."""
    plan = _make_plan(
        edit_scope=["src", "tests"],
        phases=[_phase([_t("1.1", files=["src/foo.py", "tests/test_foo.py"])])],
    )
    validate_edit_scope(plan)


def test_validate_edit_scope_raises_on_out_of_scope_file() -> None:
    """A file outside the scope raises EditScopeViolation with task + file + scope details."""
    plan = _make_plan(
        edit_scope=["src/"],
        phases=[
            _phase([
                _t("1.1", files=["src/ok.py"]),
                _t("1.2", files=["docs/out_of_scope.md"]),
            ])
        ],
    )
    with pytest.raises(EditScopeViolation) as excinfo:
        validate_edit_scope(plan)
    msg = str(excinfo.value)
    assert "1.2" in msg
    assert "docs/out_of_scope.md" in msg


def test_validate_edit_scope_phase_override_takes_precedence() -> None:
    """When ``Phase.edit_scope`` is non-None, it overrides Plan.edit_scope.

    Plan scope is broad (``src/``), but the phase narrows to ``src/foo/``.
    A task touching ``src/bar.py`` is in plan-scope but out of
    phase-scope — must raise.
    """
    plan = _make_plan(
        edit_scope=["src"],
        phases=[
            Phase(
                id="1",
                title="Narrow",
                tasks=[_t("1.1", files=["src/bar.py"])],
                edit_scope=["src/foo"],
            )
        ],
    )
    with pytest.raises(EditScopeViolation):
        validate_edit_scope(plan)


def test_validate_edit_scope_phase_empty_list_means_whole_repo_for_phase() -> None:
    """``Phase.edit_scope == []`` (explicit empty list, not None) means the
    phase opts into legacy whole-repo behavior even if the plan narrows."""
    plan = _make_plan(
        edit_scope=["src"],
        phases=[
            Phase(
                id="1",
                title="Wide",
                tasks=[_t("1.1", files=["docs/anything.md"])],
                edit_scope=[],
            )
        ],
    )
    # Should not raise — phase explicitly opts back into whole-repo.
    validate_edit_scope(plan)


def test_is_in_scope_prefix_match() -> None:
    """``is_in_scope`` does prefix matching against repo-relative paths."""
    assert is_in_scope("src/foo.py", ["src"]) is True
    assert is_in_scope("src/foo.py", ["src/"]) is True  # caller may pass un-normalized
    assert is_in_scope("src/sub/deep.py", ["src/sub"]) is True
    assert is_in_scope("docs/foo.md", ["src"]) is False


def test_is_in_scope_empty_scope_returns_true() -> None:
    """Empty scope → no constraint → every path is in scope."""
    assert is_in_scope("anywhere/at/all.py", []) is True


def test_is_in_scope_does_not_partial_match_filename_prefix() -> None:
    """Scope ``src`` should match ``src/x.py`` but NOT ``srcfoo.py`` (sibling
    starting with same letters)."""
    assert is_in_scope("src/x.py", ["src"]) is True
    assert is_in_scope("srcfoo.py", ["src"]) is False
