"""Tests for v0.11.0 DAG-aware parallel execute_phase dispatcher.

Validates the worker-pool semantics:

1. Independent tasks within a phase run concurrently.
2. Dependent tasks wait for parents to be terminal.
3. Tasks with overlapping ``files`` are serialized.
4. The worker-pool cap is respected.
5. Failed tasks cascade-block dependents.
6. Phase-review fires only after every worker drains (no double-fire).

The tests use a monkey-patched ``_execute_one`` so we don't need real
adapters / git / FSM walking — the dispatcher's contract is "spawn N,
wait, drain, mark blocked-descendants on exception" and that's what we
verify.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import time
from pathlib import Path
from typing import Any

import pytest

from orchestrator import execute_phase as ep
from state.plan_manager import PlanManager
from state.schemas import Phase, Plan, Task


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _t(tid: str, deps: list[str] | None = None, files: list[str] | None = None) -> Task:
    return Task(
        id=tid,
        phase_id="1",
        title=f"task {tid}",
        description=f"do {tid}",
        depends_on=list(deps or []),
        files=list(files or []),
    )


def _mk_plan(tasks: list[Task]) -> Plan:
    return Plan(
        plan_id="p-parallel",
        spec_hash="cafe",
        phases=[Phase(id="1", title="parallel", tasks=tasks)],
        created_at=_iso(),
        updated_at=_iso(),
    )


def _make_orch(tmp_path: Path, pm: PlanManager) -> Any:
    """Minimal orchestrator stub for the dispatcher."""
    from config.defaults import default_config

    cfg = default_config()
    # Force a small parallelism cap for deterministic tests.
    cfg.tournaments.execute_max_parallel_tasks = 4
    cfg.tournaments.phase_review.enabled = False  # Skip phase-review.

    class FakeGuard:
        def start_task(self, tid: str) -> None:
            pass

        def end_task(self, tid: str) -> None:
            pass

        def pre_invocation(self, *a, **kw) -> None:
            pass

        def post_invocation(self, *a, **kw) -> None:
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
    return orch


def _patch_execute_one(
    monkeypatch: pytest.MonkeyPatch,
    *,
    record: list[tuple[str, float, float]] | None = None,
    sleep_s: float = 0.0,
    fail_ids: set[str] | None = None,
) -> None:
    """Replace ``_execute_one`` with a fake that walks FSM to complete.

    ``record``: optional list to receive (task_id, start_ts, end_ts).
    ``sleep_s``: how long the fake task takes (lets us measure parallelism).
    ``fail_ids``: task ids that should raise instead of complete.
    """
    fail_ids = fail_ids or set()

    async def fake_execute_one(
        orch: Any, task: Task, worktree_mgr: Any = None
    ) -> Task:
        start = time.monotonic()
        if sleep_s > 0:
            await asyncio.sleep(sleep_s)
        end = time.monotonic()
        if record is not None:
            record.append((task.id, start, end))
        if task.id in fail_ids:
            raise RuntimeError(f"intentional failure of {task.id}")
        # Walk FSM to complete.
        for status in (
            "in_progress",
            "coded",
            "auto_gated",
            "reviewed",
            "tested",
            "tournamented",
            "complete",
        ):
            await orch.plan_manager.update_task_status(task.id, status)
        return (await orch.plan_manager.get_task(task.id)) or task

    monkeypatch.setattr(ep, "_execute_one", fake_execute_one)


@pytest.mark.asyncio
async def test_two_independent_tasks_run_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two independent tasks should overlap in time when parallelism>=2."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan([_t("1.1"), _t("1.2")]))
    orch = _make_orch(tmp_path, pm)
    record: list[tuple[str, float, float]] = []
    _patch_execute_one(monkeypatch, record=record, sleep_s=0.05)

    await ep.run_execute_phase(orch)

    # Two records, and their windows OVERLAP.
    assert len(record) == 2
    by_id = {tid: (s, e) for tid, s, e in record}
    a = by_id["1.1"]
    b = by_id["1.2"]
    overlap_start = max(a[0], b[0])
    overlap_end = min(a[1], b[1])
    assert overlap_end > overlap_start, (
        f"expected overlap; got {a=} {b=}"
    )


@pytest.mark.asyncio
async def test_dependent_task_waits_for_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task with depends_on=[parent] runs strictly AFTER parent completes."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan([_t("1.1"), _t("1.2", deps=["1.1"])]))
    orch = _make_orch(tmp_path, pm)
    record: list[tuple[str, float, float]] = []
    _patch_execute_one(monkeypatch, record=record, sleep_s=0.05)

    await ep.run_execute_phase(orch)

    by_id = {tid: (s, e) for tid, s, e in record}
    # 1.2 starts AFTER 1.1 ends.
    assert by_id["1.2"][0] >= by_id["1.1"][1] - 0.001  # tiny jitter tolerance


@pytest.mark.asyncio
async def test_overlapping_files_serializes_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tasks sharing a Files: entry don't run concurrently (file-overlap guard)."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan(
            [
                _t("1.1", files=["src/foo.py"]),
                _t("1.2", files=["src/foo.py"]),  # overlaps with 1.1
            ]
        )
    )
    orch = _make_orch(tmp_path, pm)
    record: list[tuple[str, float, float]] = []
    _patch_execute_one(monkeypatch, record=record, sleep_s=0.05)

    await ep.run_execute_phase(orch)

    by_id = {tid: (s, e) for tid, s, e in record}
    # No overlap — one finishes before the other starts.
    a, b = by_id["1.1"], by_id["1.2"]
    assert a[1] <= b[0] + 0.005 or b[1] <= a[0] + 0.005


@pytest.mark.asyncio
async def test_worker_pool_respects_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3 tasks with cap=2 → at most 2 run concurrently."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan([_t("1.1"), _t("1.2"), _t("1.3")]))
    orch = _make_orch(tmp_path, pm)
    orch.cfg.tournaments.execute_max_parallel_tasks = 2
    record: list[tuple[str, float, float]] = []
    _patch_execute_one(monkeypatch, record=record, sleep_s=0.08)

    await ep.run_execute_phase(orch)

    # Compute peak concurrency by walking events.
    events: list[tuple[float, int]] = []  # (ts, +1 or -1)
    for _, s, e in record:
        events.append((s, 1))
        events.append((e, -1))
    events.sort()
    cur = 0
    peak = 0
    for _, delta in events:
        cur += delta
        peak = max(peak, cur)
    assert peak <= 2, f"expected peak <= 2, got {peak}"


@pytest.mark.asyncio
async def test_failed_task_cascade_blocks_dependent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worker exception cascade-blocks dependent tasks."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan(
            [
                _t("1.1"),
                _t("1.2", deps=["1.1"]),
                _t("1.3"),  # independent — should still run
            ]
        )
    )
    orch = _make_orch(tmp_path, pm)
    _patch_execute_one(monkeypatch, fail_ids={"1.1"})

    await ep.run_execute_phase(orch)

    plan = await pm.load()
    assert plan is not None
    by_id = {t.id: t for t in plan.phases[0].tasks}
    # 1.1 itself: worker_exception.
    assert by_id["1.1"].status == "blocked"
    # 1.2: cascade-blocked due to upstream failure.
    assert by_id["1.2"].status == "blocked"
    assert (
        by_id["1.2"].blocked_reason is not None
        and "upstream-failure:1.1" in by_id["1.2"].blocked_reason
    )
    # 1.3: unaffected (independent), runs to complete.
    assert by_id["1.3"].status == "complete"


@pytest.mark.asyncio
async def test_maybe_run_phase_review_skips_when_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v0.11.0 commit 11: _maybe_run_phase_review returns early when any
    task in the phase is currently in-flight, even if the others are
    terminal. Without this guard, a worker that finishes early could
    fire phase-review while siblings are still running."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan([_t("1.1"), _t("1.2")]))
    orch = _make_orch(tmp_path, pm)
    orch.cfg.tournaments.phase_review.enabled = True

    # Walk 1.1 to complete WITHOUT clearing in_flight on 1.2.
    for status in (
        "in_progress",
        "coded",
        "auto_gated",
        "reviewed",
        "tested",
        "tournamented",
        "complete",
    ):
        await pm.update_task_status("1.1", status)
    # Mark 1.2 as in_progress + in_flight.
    await pm.update_task_status("1.2", "in_progress")
    await pm.mark_in_flight("1.2")

    fired: list[str] = []

    async def stub_run_phase_review(orch_, phase):
        fired.append(phase.id)

    monkeypatch.setattr(ep, "_run_phase_review", stub_run_phase_review)

    # 1.1 is complete, 1.2 is in_progress + in_flight → guard returns.
    await ep._maybe_run_phase_review(orch, "1")
    assert fired == []

    # Clear in_flight → guard now allows the call (but 1.2 is not
    # terminal so the all-terminal check still defers).
    await pm.clear_in_flight("1.2")
    await ep._maybe_run_phase_review(orch, "1")
    # Still not fired because 1.2 isn't terminal.
    assert fired == []


@pytest.mark.asyncio
async def test_phase_review_does_not_double_fire_on_concurrent_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two workers completing simultaneously must not fire phase_review twice."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan([_t("1.1"), _t("1.2")]))
    orch = _make_orch(tmp_path, pm)
    orch.cfg.tournaments.phase_review.enabled = True
    _patch_execute_one(monkeypatch, sleep_s=0.02)

    fired: list[str] = []

    async def fake_review(orch_, phase_id):
        fired.append(phase_id)
        # Mark accepted to avoid corrective-loop.
        await orch_.plan_manager.update_phase_meta(
            phase_id, review_status="accepted"
        )

    monkeypatch.setattr(ep, "_maybe_run_phase_review", fake_review)

    await ep.run_execute_phase(orch)

    # Phase 1 review fires exactly once even with two workers finishing
    # in close succession.
    assert fired.count("1") == 1


# ---------------------------------------------------------------------------
# v0.11.0 commit 15 — DAG validator hook in run_execute_phase
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_execute_phase_blocks_phase_on_dag_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A phase with a DAG cycle has all pending tasks marked blocked
    with reason=dag_invalid before the worker pool engages."""
    pm = PlanManager(tmp_path, session_id="s1")
    # Phase with a cycle: 1.1 → 1.2 → 1.1.
    await pm.init_plan(
        _mk_plan(
            [
                _t("1.1", deps=["1.2"]),
                _t("1.2", deps=["1.1"]),
            ]
        )
    )
    orch = _make_orch(tmp_path, pm)
    _patch_execute_one(monkeypatch)

    await ep.run_execute_phase(orch)

    plan = await pm.load()
    assert plan is not None
    by_id = {t.id: t for t in plan.phases[0].tasks}
    # Both tasks blocked with dag_invalid.
    for tid in ("1.1", "1.2"):
        assert by_id[tid].status == "blocked"
        assert (
            by_id[tid].blocked_reason is not None
            and "dag_invalid" in by_id[tid].blocked_reason
        )


@pytest.mark.asyncio
async def test_run_execute_phase_undefined_dep_blocks_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task referencing an undefined depends_on id triggers DAG error
    and the phase is blocked with the underlying error in blocked_reason."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan(
            [
                _t("1.1"),
                _t("1.2", deps=["1.999"]),  # undefined
            ]
        )
    )
    orch = _make_orch(tmp_path, pm)
    _patch_execute_one(monkeypatch)

    await ep.run_execute_phase(orch)

    plan = await pm.load()
    assert plan is not None
    by_id = {t.id: t for t in plan.phases[0].tasks}
    # Both pending tasks blocked.
    for tid in ("1.1", "1.2"):
        assert by_id[tid].status == "blocked"
