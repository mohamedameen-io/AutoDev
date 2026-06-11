"""v0.39.0 (Cluster C2c): unit tests for ``maybe_emit_under_decomposed_runtime``.

The runtime telemetry fires a best-effort ``task_under_decomposed`` ledger op
(``source="runtime"``) when a developer task busts its scaled budget with
``error_max_turns`` on a huge repo on its first one or two attempts. It is
purely observational — it must never raise and never change control flow.
"""

from __future__ import annotations

import pytest

from adapters.types import AgentResult
from orchestrator.execute_phase import maybe_emit_under_decomposed_runtime
from state.schemas import Task


def _task(*, retry_count: int = 0) -> Task:
    return Task(
        id="1.1",
        phase_id="1",
        title="t",
        description="d",
        complexity="complex",
        files=["a.py", "b.py", "c.py"],
        retry_count=retry_count,
    )


def _result(*, subtype: str | None = "error_max_turns") -> AgentResult:
    return AgentResult(
        text="",
        success=False,
        duration_s=1.0,
        files_changed=[],
        diff="",
        subtype=subtype,
    )


def _orch(*, is_huge: bool, ledger_ops: list | None) -> object:
    class FakePlanManager:
        async def ledger_append(self, *, op, payload):
            if ledger_ops is not None:
                ledger_ops.append((op, payload))

    class FakeCapacity:
        def __init__(self, h: bool) -> None:
            self.is_huge = h

    return type(
        "OrchStub",
        (),
        {
            "_repo_capacity": FakeCapacity(is_huge),
            "plan_manager": FakePlanManager() if ledger_ops is not None else None,
        },
    )()


@pytest.mark.asyncio
async def test_emits_on_max_turns_huge_attempt0() -> None:
    ledger_ops: list = []
    orch = _orch(is_huge=True, ledger_ops=ledger_ops)
    emitted = await maybe_emit_under_decomposed_runtime(
        orch, _task(retry_count=0), _result()
    )
    assert emitted is True
    ops = [p for (op, p) in ledger_ops if op == "task_under_decomposed"]
    assert len(ops) == 1
    assert ops[0]["source"] == "runtime"
    assert ops[0]["attempt"] == 0
    assert ops[0]["file_count"] == 3
    assert ops[0]["complexity"] == "complex"


@pytest.mark.asyncio
async def test_emits_on_attempt1() -> None:
    ledger_ops: list = []
    orch = _orch(is_huge=True, ledger_ops=ledger_ops)
    assert (
        await maybe_emit_under_decomposed_runtime(
            orch, _task(retry_count=1), _result()
        )
        is True
    )


@pytest.mark.asyncio
async def test_no_emit_when_subtype_not_max_turns() -> None:
    ledger_ops: list = []
    orch = _orch(is_huge=True, ledger_ops=ledger_ops)
    assert (
        await maybe_emit_under_decomposed_runtime(
            orch, _task(retry_count=0), _result(subtype="error_other")
        )
        is False
    )
    assert not ledger_ops


@pytest.mark.asyncio
async def test_no_emit_when_retry_count_ge_2() -> None:
    ledger_ops: list = []
    orch = _orch(is_huge=True, ledger_ops=ledger_ops)
    assert (
        await maybe_emit_under_decomposed_runtime(
            orch, _task(retry_count=2), _result()
        )
        is False
    )
    assert not ledger_ops


@pytest.mark.asyncio
async def test_no_emit_when_not_huge() -> None:
    ledger_ops: list = []
    orch = _orch(is_huge=False, ledger_ops=ledger_ops)
    assert (
        await maybe_emit_under_decomposed_runtime(
            orch, _task(retry_count=0), _result()
        )
        is False
    )
    assert not ledger_ops


@pytest.mark.asyncio
async def test_no_plan_manager_is_noop() -> None:
    orch = _orch(is_huge=True, ledger_ops=None)  # plan_manager=None
    assert (
        await maybe_emit_under_decomposed_runtime(
            orch, _task(retry_count=0), _result()
        )
        is False
    )


@pytest.mark.asyncio
async def test_ledger_failure_never_raises() -> None:
    class ExplodingPlanManager:
        async def ledger_append(self, *, op, payload):
            raise RuntimeError("ledger down")

    class FakeCapacity:
        is_huge = True

    orch = type(
        "OrchStub",
        (),
        {"_repo_capacity": FakeCapacity(), "plan_manager": ExplodingPlanManager()},
    )()
    # Must not raise; returns False on swallowed error.
    assert (
        await maybe_emit_under_decomposed_runtime(
            orch, _task(retry_count=0), _result()
        )
        is False
    )
