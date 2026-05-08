"""v0.21.0 B2 — speculative-execution rollback tests.

Validates :class:`PlanManager.speculable_candidate` and
:func:`orchestrator.speculative.rollback_speculative_task`:

* parent in-flight + retry==0 + single-parent child + file-disjoint
  → returns the candidate,
* parent retry_count==1 → no candidate (retry guard),
* multi-parent diamond → no candidate,
* file overlap with in-flight → no candidate,
* parent terminal → no candidate (only in-flight parents qualify),
* rollback re-queues the speculative task as ``"pending"`` and emits
  the ledger op,
* commit emits the ledger op without state mutation.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from orchestrator.speculative import (
    commit_speculative_task,
    rollback_speculative_task,
)
from state.plan_manager import PlanManager
from state.schemas import Phase, Plan, Task


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _t(
    tid: str,
    deps: list[str] | None = None,
    files: list[str] | None = None,
    retry_count: int = 0,
) -> Task:
    return Task(
        id=tid,
        phase_id="1",
        title=f"task {tid}",
        description=f"do {tid}",
        depends_on=list(deps or []),
        files=list(files or []),
        retry_count=retry_count,
    )


def _mk_plan(tasks: list[Task]) -> Plan:
    return Plan(
        plan_id="p-spec",
        spec_hash="cafe",
        phases=[Phase(id="1", title="p", tasks=tasks)],
        created_at=_iso(),
        updated_at=_iso(),
    )


# ---------------------------------------------------------------------------
# speculable_candidate happy path + guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_speculable_candidate_returns_eligible_child(
    tmp_path: Path,
) -> None:
    """Parent in-flight + retry==0 + single-parent child + disjoint files."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan(
            [
                _t("1.1", files=["src/a.py"]),
                _t("1.2", deps=["1.1"], files=["src/b.py"]),
            ]
        )
    )
    # Mark 1.1 in-flight (in_progress).
    await pm.update_task_status("1.1", "in_progress")
    await pm.mark_in_flight("1.1")

    out = await pm.speculable_candidate("1.1")
    assert out is not None
    assert out.id == "1.2"


@pytest.mark.asyncio
async def test_speculable_candidate_blocks_when_parent_retried(
    tmp_path: Path,
) -> None:
    """Parent.retry_count != 0 → no candidate."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan(
            [
                _t("1.1", files=["src/a.py"], retry_count=1),
                _t("1.2", deps=["1.1"], files=["src/b.py"]),
            ]
        )
    )
    await pm.update_task_status("1.1", "in_progress")
    await pm.mark_in_flight("1.1")
    out = await pm.speculable_candidate("1.1")
    assert out is None


@pytest.mark.asyncio
async def test_speculable_candidate_blocks_diamond_dep(
    tmp_path: Path,
) -> None:
    """Child with multiple parents (diamond) → no candidate."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan(
            [
                _t("1.1", files=["src/a.py"]),
                _t("1.2", files=["src/b.py"]),
                # Diamond: 1.3 depends on BOTH 1.1 and 1.2.
                _t("1.3", deps=["1.1", "1.2"], files=["src/c.py"]),
            ]
        )
    )
    await pm.update_task_status("1.1", "in_progress")
    await pm.mark_in_flight("1.1")
    out = await pm.speculable_candidate("1.1")
    assert out is None


@pytest.mark.asyncio
async def test_speculable_candidate_blocks_file_overlap(
    tmp_path: Path,
) -> None:
    """Child files overlap an in-flight task's files → no candidate."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan(
            [
                _t("1.1", files=["src/a.py"]),
                _t("1.2", files=["src/b.py"]),
                # 1.3 depends on 1.1 but its files overlap 1.2's.
                _t("1.3", deps=["1.1"], files=["src/b.py"]),
            ]
        )
    )
    await pm.update_task_status("1.1", "in_progress")
    await pm.mark_in_flight("1.1")
    await pm.update_task_status("1.2", "in_progress")
    await pm.mark_in_flight("1.2")
    out = await pm.speculable_candidate("1.1")
    assert out is None


@pytest.mark.asyncio
async def test_speculable_candidate_no_parent_terminal(
    tmp_path: Path,
) -> None:
    """Parent terminal → no candidate (only in-flight parents qualify)."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan(
            [
                _t("1.1", files=["src/a.py"]),
                _t("1.2", deps=["1.1"], files=["src/b.py"]),
            ]
        )
    )
    # Walk parent to terminal.
    for s in (
        "in_progress",
        "coded",
        "auto_gated",
        "reviewed",
        "tested",
        "tournamented",
        "complete",
    ):
        await pm.update_task_status("1.1", s)
    out = await pm.speculable_candidate("1.1")
    assert out is None


# ---------------------------------------------------------------------------
# Rollback + commit handlers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollback_speculative_task_requeues_and_emits_op(
    tmp_path: Path,
) -> None:
    """Rollback transitions task → pending and emits ledger entry."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan(
            [
                _t("1.1", files=["src/a.py"]),
                _t("1.2", deps=["1.1"], files=["src/b.py"]),
            ]
        )
    )
    # 1.2 was speculatively started → in_progress (any non-terminal).
    await pm.update_task_status("1.2", "in_progress")

    class OrchStub:
        plan_manager = pm

    speculative_task = _t("1.2", deps=["1.1"], files=["src/b.py"])

    await rollback_speculative_task(
        OrchStub(),  # type: ignore[arg-type]
        speculative_task,
        parent_task_id="1.1",
        reason="parent_failed_qa",
    )

    plan = await pm.load()
    assert plan is not None
    by_id = {t.id: t for t in plan.phases[0].tasks}
    # Speculative task back to pending.
    assert by_id["1.2"].status == "pending"
    assert (
        by_id["1.2"].blocked_reason is not None
        and "speculative_rollback" in (by_id["1.2"].blocked_reason or "")
    )

    # Ledger has speculative_rolled_back op.
    entries = await pm.read_ledger()
    ops = [e.op for e in entries]
    assert "speculative_rolled_back" in ops


@pytest.mark.asyncio
async def test_speculative_dispatcher_starts_child_during_parent_inflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cross-phase + speculative — a child task starts while parent in-flight.

    Plan: 1.1 (slow), 1.2 (deps=[1.1], disjoint files). With speculative
    enabled, 1.2 should start BEFORE 1.1 transitions to terminal.
    """
    import asyncio
    import time
    from typing import Any

    from config.defaults import default_config
    from orchestrator import execute_phase as ep

    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan(
            [
                _t("1.1", files=["src/a.py"]),
                _t("1.2", deps=["1.1"], files=["src/b.py"]),
            ]
        )
    )

    cfg = default_config()
    cfg.tournaments.execute_max_parallel_tasks = 4
    cfg.tournaments.phase_review.enabled = False
    cfg.cross_phase_parallelism_enabled = True
    cfg.speculative_execution_enabled = True

    class FakeGuard:
        def start_task(self, t: str) -> None:
            pass

        def end_task(self, t: str) -> None:
            pass

        def pre_invocation(self, *a: Any, **kw: Any) -> None:
            pass

        def post_invocation(self, *a: Any, **kw: Any) -> None:
            pass

    orch = type(
        "OrchStub",
        (),
        {
            "cwd": tmp_path,
            "session_id": "test",
            "plan_manager": pm,
            "cfg": cfg,
            "guardrails": FakeGuard(),
            "adapter": None,
            "registry": None,
            "knowledge": None,
            "loop_detector": None,
            "plugin_registry": None,
            "disable_impl_tournament": True,
        },
    )()

    record: list[tuple[str, float, float]] = []

    async def fake_execute_one(
        orch_arg: Any, task: Task, worktree_mgr: Any = None
    ) -> Task:
        start = time.monotonic()
        # 1.1 takes long, 1.2 short — speculative 1.2 should overlap.
        sleep_s = 0.20 if task.id == "1.1" else 0.03
        await asyncio.sleep(sleep_s)
        end = time.monotonic()
        record.append((task.id, start, end))
        for status in (
            "in_progress",
            "coded",
            "auto_gated",
            "reviewed",
            "tested",
            "tournamented",
            "complete",
        ):
            await orch_arg.plan_manager.update_task_status(task.id, status)
        return (await orch_arg.plan_manager.get_task(task.id)) or task

    monkeypatch.setattr(ep, "_execute_one", fake_execute_one)

    await ep.run_execute_phase(orch)

    by_id = {tid: (s, e) for tid, s, e in record}
    # 1.2 starts BEFORE 1.1 ends (speculative overlap).
    assert by_id["1.2"][0] < by_id["1.1"][1] - 0.001, (
        f"expected speculative overlap; got 1.2 start={by_id['1.2'][0]:.3f}, "
        f"1.1 end={by_id['1.1'][1]:.3f}"
    )
    # Ledger shows speculative_started.
    entries = await pm.read_ledger()
    ops = [e.op for e in entries]
    assert "speculative_started" in ops


@pytest.mark.asyncio
async def test_commit_speculative_task_emits_op(tmp_path: Path) -> None:
    """Commit emits ledger entry without mutating plan state."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan([_t("1.1"), _t("1.2", deps=["1.1"])]))

    class OrchStub:
        plan_manager = pm

    await commit_speculative_task(
        OrchStub(),  # type: ignore[arg-type]
        speculative_task_id="1.2",
        parent_task_id="1.1",
    )

    entries = await pm.read_ledger()
    ops = [e.op for e in entries]
    assert "speculative_committed" in ops
