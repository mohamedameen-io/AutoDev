"""v0.17.0 S6: execute_phase passes ``sparse_paths`` to per-task worktrees.

When ``cfg.worktree_sparse_checkout_enabled = True``, the per-task
worktree creation in :func:`_execute_one` resolves
``phase.edit_scope or plan.edit_scope`` and forwards it as
``sparse_paths`` into :meth:`WorktreeManager.create_per_task`.

These tests stub the WorktreeManager.create_per_task to capture the
forwarded argument without spawning real git subprocesses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from config.defaults import default_config
from orchestrator.worktree import WorktreeManager
from state.schemas import Phase, Plan, Task


def _build_plan(*, edit_scope: list[str] | None = None,
                phase_scope: list[str] | None = None) -> Plan:
    task = Task(
        id="1.1",
        phase_id="1",
        title="t",
        description="d",
        files=["src/qa/foo.py"],
    )
    phase = Phase(id="1", title="P", tasks=[task], edit_scope=phase_scope)
    return Plan(
        plan_id="p",
        spec_hash="h",
        edit_scope=edit_scope or [],
        phases=[phase],
        created_at="2026-05-08T00:00:00Z",
        updated_at="2026-05-08T00:00:00Z",
    )


@pytest.mark.asyncio
async def test_sparse_paths_forwarded_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan-level edit_scope is forwarded as sparse_paths."""
    plan = _build_plan(edit_scope=["src"])
    captured: dict[str, Any] = {}

    async def fake_create_per_task(
        self, task_id: str, base_ref: str = "HEAD",
        sparse_paths: list[str] | None = None,
        **_kw: object,
    ) -> Path:
        captured["sparse_paths"] = sparse_paths
        captured["task_id"] = task_id
        return tmp_path / "fake_wt"

    monkeypatch.setattr(
        WorktreeManager, "create_per_task", fake_create_per_task
    )

    cfg = default_config().model_copy(
        update={"worktree_sparse_checkout_enabled": True}
    )

    # Build a fake orch + plan_manager surface.
    class _FakePM:
        async def load(self) -> Plan:
            return plan

        async def update_task_status(self, *a, **kw):  # noqa: ANN001
            return plan.phases[0].tasks[0]

    class _FakeGuard:
        def start_task(self, *a, **kw): ...
        def end_task(self, *a, **kw): ...

    class _FakeOrch:
        pass

    fake_orch = _FakeOrch()
    fake_orch.cfg = cfg  # type: ignore[attr-defined]
    fake_orch.cwd = tmp_path  # type: ignore[attr-defined]
    fake_orch.plan_manager = _FakePM()  # type: ignore[attr-defined]
    fake_orch.guardrails = _FakeGuard()  # type: ignore[attr-defined]

    # Probe the worktree-creation block by calling the code path
    # directly via a minimal monkeypatched _execute_one.
    from orchestrator import execute_phase as ep

    # Stub down the rest of _execute_one to short-circuit after worktree
    # creation. We patch the developer dispatch to raise a sentinel that
    # the test can swallow.
    class _Sentinel(Exception):
        pass

    async def fake_delegate(*a, **kw):  # noqa: ANN001
        raise _Sentinel("stop after worktree create")

    monkeypatch.setattr(ep, "delegate", fake_delegate)

    wt_mgr = WorktreeManager(tmp_path, tmp_path / "wts")
    task = plan.phases[0].tasks[0]

    with pytest.raises(_Sentinel):
        await ep._execute_one(fake_orch, task, worktree_mgr=wt_mgr)

    assert captured["task_id"] == "1.1"
    assert captured["sparse_paths"] == ["src"]


@pytest.mark.asyncio
async def test_sparse_paths_none_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the flag is OFF, sparse_paths is None (full checkout)."""
    plan = _build_plan(edit_scope=["src"])
    captured: dict[str, Any] = {}

    async def fake_create_per_task(
        self, task_id: str, base_ref: str = "HEAD",
        sparse_paths: list[str] | None = None,
        **_kw: object,
    ) -> Path:
        captured["sparse_paths"] = sparse_paths
        return tmp_path / "fake_wt"

    monkeypatch.setattr(
        WorktreeManager, "create_per_task", fake_create_per_task
    )

    cfg = default_config()  # worktree_sparse_checkout_enabled = False

    class _FakePM:
        async def load(self) -> Plan:
            return plan

        async def update_task_status(self, *a, **kw):  # noqa: ANN001
            return plan.phases[0].tasks[0]

    class _FakeGuard:
        def start_task(self, *a, **kw): ...
        def end_task(self, *a, **kw): ...

    class _FakeOrch:
        pass

    fake_orch = _FakeOrch()
    fake_orch.cfg = cfg  # type: ignore[attr-defined]
    fake_orch.cwd = tmp_path  # type: ignore[attr-defined]
    fake_orch.plan_manager = _FakePM()  # type: ignore[attr-defined]
    fake_orch.guardrails = _FakeGuard()  # type: ignore[attr-defined]

    from orchestrator import execute_phase as ep

    class _Sentinel(Exception):
        pass

    async def fake_delegate(*a, **kw):  # noqa: ANN001
        raise _Sentinel("stop")

    monkeypatch.setattr(ep, "delegate", fake_delegate)

    wt_mgr = WorktreeManager(tmp_path, tmp_path / "wts")
    task = plan.phases[0].tasks[0]

    with pytest.raises(_Sentinel):
        await ep._execute_one(fake_orch, task, worktree_mgr=wt_mgr)

    assert captured["sparse_paths"] is None


@pytest.mark.asyncio
async def test_phase_scope_overrides_plan_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase.edit_scope (when non-None) wins over Plan.edit_scope."""
    plan = _build_plan(edit_scope=["src"], phase_scope=["src/qa"])
    captured: dict[str, Any] = {}

    async def fake_create_per_task(
        self, task_id: str, base_ref: str = "HEAD",
        sparse_paths: list[str] | None = None,
        **_kw: object,
    ) -> Path:
        captured["sparse_paths"] = sparse_paths
        return tmp_path / "fake_wt"

    monkeypatch.setattr(
        WorktreeManager, "create_per_task", fake_create_per_task
    )

    cfg = default_config().model_copy(
        update={"worktree_sparse_checkout_enabled": True}
    )

    class _FakePM:
        async def load(self) -> Plan:
            return plan

        async def update_task_status(self, *a, **kw):  # noqa: ANN001
            return plan.phases[0].tasks[0]

    class _FakeGuard:
        def start_task(self, *a, **kw): ...
        def end_task(self, *a, **kw): ...

    class _FakeOrch:
        pass

    fake_orch = _FakeOrch()
    fake_orch.cfg = cfg  # type: ignore[attr-defined]
    fake_orch.cwd = tmp_path  # type: ignore[attr-defined]
    fake_orch.plan_manager = _FakePM()  # type: ignore[attr-defined]
    fake_orch.guardrails = _FakeGuard()  # type: ignore[attr-defined]

    from orchestrator import execute_phase as ep

    class _Sentinel(Exception):
        pass

    async def fake_delegate(*a, **kw):  # noqa: ANN001
        raise _Sentinel("stop")

    monkeypatch.setattr(ep, "delegate", fake_delegate)

    wt_mgr = WorktreeManager(tmp_path, tmp_path / "wts")
    task = plan.phases[0].tasks[0]

    with pytest.raises(_Sentinel):
        await ep._execute_one(fake_orch, task, worktree_mgr=wt_mgr)

    assert captured["sparse_paths"] == ["src/qa"]
