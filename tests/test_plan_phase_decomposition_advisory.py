"""Tests for v0.39.0 (Cluster C2b) planner task-decomposition advisory.

``_advise_task_decomposition`` is a post-parse, pre-``init_plan`` advisory
that runs only on huge repos. For each ``complex`` task whose ``Files:`` list
is broad (>= 6 files) or whose path points at one very large file (>100KB),
it logs a warning and emits a best-effort ``task_under_decomposed`` ledger op
with ``source="planner_advisory"``. It NEVER mutates/rejects the plan and
NEVER raises.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.plan_phase import _advise_task_decomposition
from runtime.repo_probe import RepoCapacity
from state.schemas import Phase, Plan, Task


def _plan(tasks: list[Task]) -> Plan:
    phase = Phase(id="1", title="Implement", description="", tasks=tasks)
    return Plan(
        plan_id="plan-test",
        spec_hash="abc123",
        phases=[phase],
        metadata={"title": "Example"},
        complexity=None,
        edit_scope=["src"],
        created_at="2026-05-12T00:00:00+00:00",
        updated_at="2026-05-12T00:00:00+00:00",
    )


def _task(
    *,
    task_id: str = "1.1",
    complexity: str | None = "complex",
    files: list[str] | None = None,
) -> Task:
    return Task(
        id=task_id,
        phase_id="1",
        title="t",
        description="d",
        complexity=complexity,  # type: ignore[arg-type]
        files=files or [],
    )


def _orch_stub(
    tmp_path: Path, *, is_huge: bool, ledger_ops: list
) -> object:
    class FakePlanManager:
        async def ledger_append(self, *, op, payload):
            ledger_ops.append((op, payload))

    capacity = RepoCapacity(
        file_count=25_000 if is_huge else 100,
        total_bytes=1_000_000,
        depth_max=5,
        is_huge=is_huge,
    )
    return type(
        "OrchStub",
        (),
        {
            "plan_manager": FakePlanManager(),
            "_repo_capacity": capacity,
            "cwd": tmp_path,
        },
    )()


@pytest.mark.asyncio
async def test_complex_task_broad_files_emits(tmp_path: Path) -> None:
    """complex task with >= 6 files on a huge repo → emit, plan unmutated."""
    ledger_ops: list = []
    orch = _orch_stub(tmp_path, is_huge=True, ledger_ops=ledger_ops)
    files = [f"f{i}.py" for i in range(6)]
    plan = _plan([_task(files=files)])

    await _advise_task_decomposition(orch, plan, tmp_path)

    emitted = [p for (op, p) in ledger_ops if op == "task_under_decomposed"]
    assert len(emitted) == 1
    assert emitted[0]["source"] == "planner_advisory"
    assert emitted[0]["file_count"] == 6
    assert emitted[0]["complexity"] == "complex"
    assert emitted[0]["files"] == files[:10]
    # Plan returned unmutated (same object, same tasks).
    assert len(plan.phases[0].tasks) == 1
    assert plan.phases[0].tasks[0].complexity == "complex"


@pytest.mark.asyncio
async def test_non_huge_no_emit(tmp_path: Path) -> None:
    """is_huge=False → no emit even with a broad complex task."""
    ledger_ops: list = []
    orch = _orch_stub(tmp_path, is_huge=False, ledger_ops=ledger_ops)
    plan = _plan([_task(files=[f"f{i}.py" for i in range(8)])])

    await _advise_task_decomposition(orch, plan, tmp_path)

    assert not ledger_ops


@pytest.mark.asyncio
async def test_two_file_complex_no_emit(tmp_path: Path) -> None:
    """A narrow (2-file) complex task on a huge repo → no emit."""
    ledger_ops: list = []
    orch = _orch_stub(tmp_path, is_huge=True, ledger_ops=ledger_ops)
    plan = _plan([_task(files=["a.py", "b.py"])])

    await _advise_task_decomposition(orch, plan, tmp_path)

    assert not ledger_ops


@pytest.mark.asyncio
async def test_medium_task_never_emits(tmp_path: Path) -> None:
    """A broad *medium* task is not a complex-decomposition smell → no emit."""
    ledger_ops: list = []
    orch = _orch_stub(tmp_path, is_huge=True, ledger_ops=ledger_ops)
    plan = _plan(
        [_task(complexity="medium", files=[f"f{i}.py" for i in range(8)])]
    )

    await _advise_task_decomposition(orch, plan, tmp_path)

    assert not ledger_ops


@pytest.mark.asyncio
async def test_large_file_smell_emits(tmp_path: Path) -> None:
    """A narrow complex task with one >100KB file → emit (large-file smell)."""
    ledger_ops: list = []
    orch = _orch_stub(tmp_path, is_huge=True, ledger_ops=ledger_ops)
    big = tmp_path / "huge.py"
    big.write_bytes(b"x" * 200_000)
    plan = _plan([_task(files=["huge.py", "small.py"])])

    await _advise_task_decomposition(orch, plan, tmp_path)

    emitted = [p for (op, p) in ledger_ops if op == "task_under_decomposed"]
    assert len(emitted) == 1
    assert emitted[0]["file_count"] == 2


@pytest.mark.asyncio
async def test_glob_path_not_statted(tmp_path: Path) -> None:
    """Glob/pattern paths are skipped by the large-file check (no OSError)."""
    ledger_ops: list = []
    orch = _orch_stub(tmp_path, is_huge=True, ledger_ops=ledger_ops)
    # Glob path + a small literal: neither triggers a smell → no emit.
    plan = _plan([_task(files=["src/**/*.py", "small.py"])])

    await _advise_task_decomposition(orch, plan, tmp_path)

    assert not ledger_ops


@pytest.mark.asyncio
async def test_ledger_failure_never_raises(tmp_path: Path) -> None:
    """A ledger_append that raises must be swallowed (advisory never breaks)."""

    class ExplodingPlanManager:
        async def ledger_append(self, *, op, payload):
            raise RuntimeError("ledger down")

    capacity = RepoCapacity(
        file_count=25_000, total_bytes=1_000_000, depth_max=5, is_huge=True
    )
    orch = type(
        "OrchStub",
        (),
        {
            "plan_manager": ExplodingPlanManager(),
            "_repo_capacity": capacity,
            "cwd": tmp_path,
        },
    )()
    plan = _plan([_task(files=[f"f{i}.py" for i in range(6)])])

    # Must not raise.
    await _advise_task_decomposition(orch, plan, tmp_path)
    # Plan still intact.
    assert len(plan.phases[0].tasks) == 1
