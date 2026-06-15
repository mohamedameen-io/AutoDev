"""Tests for :mod:`orchestrator.dependency_inference` (v0.41.0 A2).

Inference closes the Run-3 parallel-worktree incoherence: when a later
same-phase task consumes a file an earlier task creates/edits (or names the
earlier task's id in its description) and the architect declared no
``Depends:``, the orchestrator must add the edge so the scheduler serializes
them.
"""

from __future__ import annotations

from orchestrator.dependency_inference import (
    infer_dependencies,
    infer_plan_dependencies,
)
from state.schemas import Phase, Task


def _t(
    tid: str,
    *,
    files: list[str] | None = None,
    files_new: list[str] | None = None,
    depends_on: list[str] | None = None,
    description: str = "",
) -> Task:
    return Task(
        id=tid,
        phase_id="1",
        title=f"task {tid}",
        description=description or f"do {tid}",
        files=list(files or []),
        files_new=list(files_new or []),
        depends_on=list(depends_on or []),
    )


def _phase(tasks: list[Task]) -> Phase:
    return Phase(id="1", title="Test phase", tasks=tasks)


# ---------------------------------------------------------------------------
# file-overlap inference (the Run-3 case)
# ---------------------------------------------------------------------------


def test_infer_file_overlap_creator_then_consumer() -> None:
    """1.1 creates serialize.py; 1.2 edits it → 1.2 depends_on 1.1."""
    phase = _phase(
        [
            _t("1.1", files_new=["src/api/serialize.py"]),
            _t("1.2", files=["src/api/handler.py", "src/api/serialize.py"]),
        ]
    )
    infer_dependencies(phase)
    assert phase.tasks[1].depends_on == ["1.1"]
    # Producer is untouched.
    assert phase.tasks[0].depends_on == []


def test_infer_file_overlap_on_edited_file_not_only_created() -> None:
    """A producer that *edits* (files) a shared path also creates the edge."""
    phase = _phase(
        [
            _t("1.1", files=["src/cfg.py"]),
            _t("1.2", files=["src/cfg.py"]),
        ]
    )
    infer_dependencies(phase)
    assert phase.tasks[1].depends_on == ["1.1"]


def test_no_inference_when_files_disjoint() -> None:
    phase = _phase(
        [
            _t("1.1", files_new=["src/a.py"]),
            _t("1.2", files=["src/b.py"]),
        ]
    )
    infer_dependencies(phase)
    assert phase.tasks[0].depends_on == []
    assert phase.tasks[1].depends_on == []


def test_explicit_depends_on_is_never_overridden() -> None:
    """A task the architect already ordered is left exactly as declared."""
    phase = _phase(
        [
            _t("1.1", files_new=["src/api/serialize.py"]),
            _t("1.0b", files=["src/api/serialize.py"]),
            _t(
                "1.2",
                files=["src/api/serialize.py"],
                depends_on=["1.0b"],
            ),
        ]
    )
    infer_dependencies(phase)
    # 1.2 had an explicit dep → untouched (NOT widened to include 1.1).
    assert phase.tasks[2].depends_on == ["1.0b"]


def test_inference_only_points_backward_no_cycle() -> None:
    """Edges only go later→earlier, so the resulting DAG is acyclic."""
    phase = _phase(
        [
            _t("1.1", files_new=["src/x.py"]),
            _t("1.2", files=["src/x.py", "src/y.py"]),
            _t("1.3", files=["src/y.py"]),
        ]
    )
    infer_dependencies(phase)
    # 1.2 depends on 1.1 (shares x.py). 1.3 depends on 1.2 (shares y.py).
    assert phase.tasks[1].depends_on == ["1.1"]
    assert phase.tasks[2].depends_on == ["1.2"]
    # No task depends on a later task.
    order = {t.id: i for i, t in enumerate(phase.tasks)}
    for t in phase.tasks:
        for dep in t.depends_on:
            assert order[dep] < order[t.id]


def test_multiple_producers_emitted_in_declaration_order() -> None:
    phase = _phase(
        [
            _t("1.1", files_new=["src/a.py"]),
            _t("1.2", files_new=["src/b.py"]),
            _t("1.3", files=["src/a.py", "src/b.py"]),
        ]
    )
    infer_dependencies(phase)
    assert phase.tasks[2].depends_on == ["1.1", "1.2"]


# ---------------------------------------------------------------------------
# description-reference inference
# ---------------------------------------------------------------------------


def test_infer_description_id_reference() -> None:
    phase = _phase(
        [
            _t("1.1", description="Create the serializer in src/api/serialize.py"),
            _t(
                "1.2",
                files=["src/api/handler.py"],
                description="Route the handler through the serializer created in 1.1.",
            ),
        ]
    )
    infer_dependencies(phase)
    assert phase.tasks[1].depends_on == ["1.1"]


def test_description_reference_respects_token_boundaries() -> None:
    """A bare '1.1' must not match inside '11.1' or '1.11'."""
    phase = _phase(
        [
            _t("1.1", files_new=["src/a.py"]),
            _t(
                "1.2",
                files=["src/z.py"],
                description="See task 1.11 and 11.1 for context.",
            ),
        ]
    )
    infer_dependencies(phase)
    # Neither '1.11' nor '11.1' equals '1.1' as a token → no edge.
    assert phase.tasks[1].depends_on == []


# ---------------------------------------------------------------------------
# conservatism: globs, cross-phase, degenerate phases
# ---------------------------------------------------------------------------


def test_glob_files_are_ignored_for_overlap() -> None:
    """Glob entries are not resolved here → no inference on a glob match."""
    phase = _phase(
        [
            _t("1.1", files_new=["src/api/serialize.py"]),
            _t("1.2", files=["src/api/*.py"]),
        ]
    )
    infer_dependencies(phase)
    assert phase.tasks[1].depends_on == []


def test_single_task_phase_is_noop() -> None:
    phase = _phase([_t("1.1", files_new=["src/a.py"])])
    infer_dependencies(phase)
    assert phase.tasks[0].depends_on == []


def test_empty_phase_is_noop() -> None:
    phase = _phase([])
    assert infer_dependencies(phase) is phase


def test_infer_is_same_phase_only() -> None:
    """infer_plan_dependencies never crosses phase boundaries."""
    p1 = Phase(
        id="1",
        title="A",
        tasks=[_t("1.1", files_new=["src/shared.py"])],
    )
    # 2.1 shares a path with 1.1 but lives in a different phase.
    p2 = Phase(
        id="2",
        title="B",
        tasks=[
            Task(
                id="2.1",
                phase_id="2",
                title="t",
                description="d",
                files=["src/shared.py"],
            ),
            Task(
                id="2.2",
                phase_id="2",
                title="t",
                description="d",
                files=["src/shared.py"],
            ),
        ],
    )
    infer_plan_dependencies([p1, p2])
    # No cross-phase edge: 2.1 does not depend on 1.1.
    assert p2.tasks[0].depends_on == []
    # In-phase edge still fires: 2.2 depends on 2.1.
    assert p2.tasks[1].depends_on == ["2.1"]


def test_returns_same_phase_object() -> None:
    phase = _phase(
        [
            _t("1.1", files_new=["src/a.py"]),
            _t("1.2", files=["src/a.py"]),
        ]
    )
    assert infer_dependencies(phase) is phase
