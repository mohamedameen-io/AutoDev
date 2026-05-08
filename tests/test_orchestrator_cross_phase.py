"""v0.21.0 B1 — cross-phase parallelism dispatcher tests.

Validates :func:`orchestrator.execute_phase._execute_cross_phase_dag` and
:func:`_maybe_record_phase_checkpoint`:

* phase 2's first task starts before phase 1's last task completes
  (cross-phase scheduling with disjoint files),
* phase-review uses ``Phase.end_checkpoint_commit`` for diff range,
  NOT live HEAD (so concurrent phase-N+1 commits don't pollute the
  phase-N review),
* file-overlap guard still serializes cross-phase tasks that share
  files,
* cross-phase parallelism off (default) preserves byte-identical
  serial-phase behavior.
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


def _t(
    tid: str,
    phase_id: str,
    deps: list[str] | None = None,
    files: list[str] | None = None,
) -> Task:
    return Task(
        id=tid,
        phase_id=phase_id,
        title=f"task {tid}",
        description=f"do {tid}",
        depends_on=list(deps or []),
        files=list(files or []),
    )


def _mk_two_phase_plan(
    p1_tasks: list[Task], p2_tasks: list[Task]
) -> Plan:
    return Plan(
        plan_id="p-cross-phase",
        spec_hash="cafe",
        phases=[
            Phase(id="1", title="phase-1", tasks=p1_tasks),
            Phase(id="2", title="phase-2", tasks=p2_tasks),
        ],
        created_at=_iso(),
        updated_at=_iso(),
    )


def _make_orch(
    tmp_path: Path, pm: PlanManager, *, cross_phase: bool = True
) -> Any:
    """Minimal orchestrator stub with cross-phase parallelism toggle."""
    from config.defaults import default_config

    cfg = default_config()
    cfg.tournaments.execute_max_parallel_tasks = 4
    cfg.tournaments.phase_review.enabled = False
    cfg.cross_phase_parallelism_enabled = cross_phase

    class FakeGuard:
        def start_task(self, tid: str) -> None:
            pass

        def end_task(self, tid: str) -> None:
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
    return orch


def _patch_execute_one(
    monkeypatch: pytest.MonkeyPatch,
    *,
    record: list[tuple[str, float, float]] | None = None,
    sleep_s: float = 0.05,
    per_task_sleep: dict[str, float] | None = None,
) -> None:
    async def fake_execute_one(
        orch: Any, task: Task, worktree_mgr: Any = None
    ) -> Task:
        start = time.monotonic()
        s = (
            per_task_sleep.get(task.id, sleep_s)
            if per_task_sleep is not None
            else sleep_s
        )
        if s > 0:
            await asyncio.sleep(s)
        end = time.monotonic()
        if record is not None:
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
            await orch.plan_manager.update_task_status(task.id, status)
        return (await orch.plan_manager.get_task(task.id)) or task

    monkeypatch.setattr(ep, "_execute_one", fake_execute_one)


# ---------------------------------------------------------------------------
# Cross-phase scheduling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_phase_task_starts_before_prior_phase_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 2's task starts before phase 1's last task completes.

    Plan: phase 1 has [1.1 (slow), 1.2 (slow, no deps)]; phase 2 has
    [2.1 (depends on 1.1, disjoint files)]. With cross-phase enabled,
    2.1 should start as soon as 1.1 is terminal — 1.2 may still be
    running.
    """
    pm = PlanManager(tmp_path, session_id="s1")
    plan = _mk_two_phase_plan(
        p1_tasks=[
            _t("1.1", "1", files=["src/a.py"]),
            _t("1.2", "1", files=["src/b.py"]),
        ],
        p2_tasks=[
            _t("2.1", "2", deps=["1.1"], files=["src/c.py"]),
        ],
    )
    await pm.init_plan(plan)
    orch = _make_orch(tmp_path, pm, cross_phase=True)
    record: list[tuple[str, float, float]] = []
    # 1.1 finishes fast, 1.2 takes longer — 2.1 should start as soon as
    # 1.1 is terminal, while 1.2 is still in flight.
    _patch_execute_one(
        monkeypatch,
        record=record,
        per_task_sleep={"1.1": 0.03, "1.2": 0.20, "2.1": 0.03},
    )

    await ep.run_execute_phase(orch)

    by_id = {tid: (s, e) for tid, s, e in record}
    # 2.1 starts AFTER 1.1 ends (its dep).
    assert by_id["2.1"][0] >= by_id["1.1"][1] - 0.001
    # 2.1 starts BEFORE 1.2 ends (overlap = cross-phase).
    assert by_id["2.1"][0] < by_id["1.2"][1] - 0.001, (
        f"expected cross-phase overlap; got 2.1 start={by_id['2.1'][0]:.3f}, "
        f"1.2 end={by_id['1.2'][1]:.3f}"
    )


@pytest.mark.asyncio
async def test_cross_phase_disabled_uses_legacy_serial_phases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cross_phase_parallelism_enabled=False → legacy serial phase order.

    Without cross-phase deps, phase 2 starts AFTER phase 1's last task
    completes (legacy serial-phase semantics).
    """
    pm = PlanManager(tmp_path, session_id="s1")
    plan = _mk_two_phase_plan(
        p1_tasks=[
            _t("1.1", "1", files=["src/a.py"]),
            _t("1.2", "1", files=["src/b.py"]),
        ],
        p2_tasks=[
            _t("2.1", "2", files=["src/c.py"]),  # no cross-phase dep
        ],
    )
    await pm.init_plan(plan)
    orch = _make_orch(tmp_path, pm, cross_phase=False)
    record: list[tuple[str, float, float]] = []
    _patch_execute_one(monkeypatch, record=record, sleep_s=0.05)

    await ep.run_execute_phase(orch)

    by_id = {tid: (s, e) for tid, s, e in record}
    # 2.1 starts AFTER both 1.1 and 1.2 complete.
    phase_1_end = max(by_id["1.1"][1], by_id["1.2"][1])
    assert by_id["2.1"][0] >= phase_1_end - 0.005


@pytest.mark.asyncio
async def test_cross_phase_file_overlap_still_serializes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cross-phase tasks sharing a Files: entry don't overlap in time."""
    pm = PlanManager(tmp_path, session_id="s1")
    plan = _mk_two_phase_plan(
        p1_tasks=[_t("1.1", "1", files=["src/shared.py"])],
        p2_tasks=[_t("2.1", "2", deps=[], files=["src/shared.py"])],
    )
    await pm.init_plan(plan)
    orch = _make_orch(tmp_path, pm, cross_phase=True)
    record: list[tuple[str, float, float]] = []
    _patch_execute_one(monkeypatch, record=record, sleep_s=0.05)

    await ep.run_execute_phase(orch)

    by_id = {tid: (s, e) for tid, s, e in record}
    a, b = by_id["1.1"], by_id["2.1"]
    # No overlap — file overlap guard serializes them.
    assert a[1] <= b[0] + 0.005 or b[1] <= a[0] + 0.005


# ---------------------------------------------------------------------------
# Phase-review uses end_checkpoint_commit (capture path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_record_phase_checkpoint_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_maybe_record_phase_checkpoint`` is idempotent + only fires when
    every task in the phase is terminal."""
    pm = PlanManager(tmp_path, session_id="s1")
    plan = _mk_two_phase_plan(
        p1_tasks=[_t("1.1", "1"), _t("1.2", "1")],
        p2_tasks=[_t("2.1", "2")],
    )
    await pm.init_plan(plan)
    orch = _make_orch(tmp_path, pm)

    # Stub _git_rev_parse_head so we don't need real git.
    monkeypatch.setattr(ep, "_git_rev_parse_head", lambda _cwd: "abcdef0123456789")

    # Phase 1 not terminal → no-op.
    await ep._maybe_record_phase_checkpoint(orch, "1")
    plan_now = await pm.load()
    assert plan_now is not None
    assert plan_now.phases[0].end_checkpoint_commit is None

    # Walk full FSM to terminal for phase 1 tasks.
    for tid in ("1.1", "1.2"):
        for status in (
            "in_progress",
            "coded",
            "auto_gated",
            "reviewed",
            "tested",
            "tournamented",
            "complete",
        ):
            await pm.update_task_status(tid, status)

    await ep._maybe_record_phase_checkpoint(orch, "1")
    plan_now = await pm.load()
    assert plan_now is not None
    assert plan_now.phases[0].end_checkpoint_commit == "abcdef0123456789"

    # Idempotent — second call doesn't change the SHA.
    monkeypatch.setattr(ep, "_git_rev_parse_head", lambda _cwd: "ffffffffffffffff")
    await ep._maybe_record_phase_checkpoint(orch, "1")
    plan_now = await pm.load()
    assert plan_now is not None
    assert plan_now.phases[0].end_checkpoint_commit == "abcdef0123456789"
