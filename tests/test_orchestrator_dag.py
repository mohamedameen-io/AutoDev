"""Tests for :mod:`orchestrator.dag` — DAG validation and scheduling helpers."""

from __future__ import annotations

import pytest

from orchestrator.dag import (
    DagValidationError,
    find_blocked_descendants,
    find_file_overlaps,
    topological_levels,
    validate_phase_dag,
)
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
