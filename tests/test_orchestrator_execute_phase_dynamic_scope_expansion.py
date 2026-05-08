"""v0.20.0 C3: dynamic scope expansion in execute_phase on missing-file errors."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orchestrator.execute_phase import _maybe_expand_sparse_for_missing
from state.schemas import Phase, Plan, Task


class _FakeWorktreeMgr:
    def __init__(self) -> None:
        self.expand_calls: list[tuple[Path, list[str]]] = []

    async def expand_sparse_paths(self, worktree: Path, paths: list[str]) -> None:
        self.expand_calls.append((worktree, list(paths)))


class _FakePlanManager:
    def __init__(self, plan: Plan) -> None:
        self._plan = plan
        self.ledger_ops: list[tuple[str, dict]] = []

    async def load(self) -> Plan:
        return self._plan

    async def ledger_append(self, op: str, payload: dict) -> None:
        self.ledger_ops.append((op, payload))


class _FakeOrch:
    def __init__(self, plan: Plan) -> None:
        self.plan_manager = _FakePlanManager(plan)


def _build_plan(
    edit_scope: list[str], task_extended: list[str] | None = None
) -> tuple[Plan, Task]:
    t = Task(
        id="1.1",
        phase_id="1",
        title="t",
        description="d",
        files=["src/orch/x.py"],
        extended_scope=task_extended or [],
    )
    plan = Plan(
        plan_id="p-1",
        spec_hash="x",
        phases=[Phase(id="1", title="P1", description="", tasks=[t])],
        edit_scope=edit_scope,
        complexity="simple",
        created_at="2024-01-01T00:00:00+00:00",
        updated_at="2024-01-01T00:00:00+00:00",
    )
    return plan, t


@pytest.mark.asyncio
async def test_expansion_admits_path_under_extended_scope(tmp_path: Path) -> None:
    """Missing file under task.extended_scope → admit + return True."""
    plan, task = _build_plan(
        edit_scope=["src/orch"], task_extended=["src/qa"]
    )
    orch = _FakeOrch(plan)
    mgr = _FakeWorktreeMgr()
    text = "FileNotFoundError: [Errno 2] No such file or directory: 'src/qa/util.py'"
    expanded = await _maybe_expand_sparse_for_missing(
        orch=orch,  # type: ignore[arg-type]
        task=task,
        worktree=tmp_path / "wt",
        worktree_mgr=mgr,  # type: ignore[arg-type]
        adapter_text=text,
    )
    assert expanded is True
    assert mgr.expand_calls == [(tmp_path / "wt", ["src/qa"])]


@pytest.mark.asyncio
async def test_expansion_skips_path_outside_any_scope(tmp_path: Path) -> None:
    """Missing path NOT covered by any scope → no expansion, return False."""
    plan, task = _build_plan(edit_scope=["src/orch"])
    orch = _FakeOrch(plan)
    mgr = _FakeWorktreeMgr()
    text = "leaks/secret.txt: No such file or directory"
    expanded = await _maybe_expand_sparse_for_missing(
        orch=orch,  # type: ignore[arg-type]
        task=task,
        worktree=tmp_path / "wt",
        worktree_mgr=mgr,  # type: ignore[arg-type]
        adapter_text=text,
    )
    assert expanded is False
    assert mgr.expand_calls == []


@pytest.mark.asyncio
async def test_expansion_with_no_missing_paths_returns_false(
    tmp_path: Path,
) -> None:
    plan, task = _build_plan(edit_scope=["src/orch"])
    orch = _FakeOrch(plan)
    mgr = _FakeWorktreeMgr()
    expanded = await _maybe_expand_sparse_for_missing(
        orch=orch,  # type: ignore[arg-type]
        task=task,
        worktree=tmp_path / "wt",
        worktree_mgr=mgr,  # type: ignore[arg-type]
        adapter_text="No errors. Test ran fine.",
    )
    assert expanded is False
    assert mgr.expand_calls == []


@pytest.mark.asyncio
async def test_expansion_empty_scope_skips_admission(tmp_path: Path) -> None:
    """When no scope is configured (whole-repo mode) we never admit
    arbitrary paths — return False."""
    plan, task = _build_plan(edit_scope=[])  # whole-repo
    orch = _FakeOrch(plan)
    mgr = _FakeWorktreeMgr()
    text = "src/qa/x.py: No such file or directory"
    expanded = await _maybe_expand_sparse_for_missing(
        orch=orch,  # type: ignore[arg-type]
        task=task,
        worktree=tmp_path / "wt",
        worktree_mgr=mgr,  # type: ignore[arg-type]
        adapter_text=text,
    )
    # No scope → safety: never widen blindly
    assert expanded is False


@pytest.mark.asyncio
async def test_expansion_admits_phase_scope_too(tmp_path: Path) -> None:
    """Missing path under phase.edit_scope (not just plan) → admit."""
    t = Task(
        id="1.1",
        phase_id="1",
        title="t",
        description="d",
        files=["src/orch/x.py"],
    )
    phase = Phase(
        id="1",
        title="P1",
        description="",
        tasks=[t],
        edit_scope=["src/qa"],  # phase-level override
    )
    plan = Plan(
        plan_id="p-1",
        spec_hash="x",
        phases=[phase],
        edit_scope=["src/orch"],
        complexity="simple",
        created_at="2024-01-01T00:00:00+00:00",
        updated_at="2024-01-01T00:00:00+00:00",
    )
    orch = _FakeOrch(plan)
    mgr = _FakeWorktreeMgr()
    text = "FileNotFoundError: [Errno 2] No such file or directory: 'src/qa/helper.py'"
    expanded = await _maybe_expand_sparse_for_missing(
        orch=orch,  # type: ignore[arg-type]
        task=t,
        worktree=tmp_path / "wt",
        worktree_mgr=mgr,  # type: ignore[arg-type]
        adapter_text=text,
    )
    assert expanded is True
    assert ("src/qa",) == tuple(mgr.expand_calls[0][1])


@pytest.mark.asyncio
async def test_expansion_writes_ledger_op(tmp_path: Path) -> None:
    plan, task = _build_plan(edit_scope=["src/orch", "src/qa"])
    orch = _FakeOrch(plan)
    mgr = _FakeWorktreeMgr()
    text = "src/qa/y.py: No such file or directory"
    await _maybe_expand_sparse_for_missing(
        orch=orch,  # type: ignore[arg-type]
        task=task,
        worktree=tmp_path / "wt",
        worktree_mgr=mgr,  # type: ignore[arg-type]
        adapter_text=text,
    )
    ops = [op for op, _ in orch.plan_manager.ledger_ops]
    assert "sparse_checkout_expanded" in ops
