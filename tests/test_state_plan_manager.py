"""Tests for :mod:`src.state.plan_manager`."""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from errors import PlanConcurrentModificationError
from state.plan_manager import PlanManager, current_plan_path, read_plan_json
from state.paths import ledger_path, plan_path
from state.schemas import Phase, Plan, Task


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan() -> Plan:
    return Plan(
        plan_id="p-test",
        spec_hash="cafebabe",
        phases=[
            Phase(
                id="1",
                title="Setup",
                tasks=[
                    Task(id="1.1", phase_id="1", title="task a", description="do a"),
                    Task(id="1.2", phase_id="1", title="task b", description="do b"),
                ],
            ),
            Phase(
                id="2",
                title="Finalize",
                tasks=[
                    Task(id="2.1", phase_id="2", title="task c", description="do c"),
                ],
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
    )


@pytest.mark.asyncio
async def test_load_returns_none_when_empty(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    assert await pm.load() is None


@pytest.mark.asyncio
async def test_init_and_load_round_trip(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    loaded = await pm.load()
    assert loaded is not None
    assert loaded.plan_id == "p-test"
    assert len(loaded.phases) == 2


@pytest.mark.asyncio
async def test_init_plan_twice_raises(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    with pytest.raises(PlanConcurrentModificationError):
        await pm.init_plan(_mk_plan())


@pytest.mark.asyncio
async def test_save_overwrites(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    # Create an edited copy with an extra task, save it.
    loaded = await pm.load()
    assert loaded is not None
    loaded.phases[0].tasks.append(
        Task(id="1.3", phase_id="1", title="new", description="new")
    )
    await pm.save(loaded)
    reloaded = await pm.load()
    assert reloaded is not None
    assert len(reloaded.phases[0].tasks) == 3


@pytest.mark.asyncio
async def test_update_task_status_enforces_fsm(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    # pending -> complete is invalid (must go through in_progress etc.)
    with pytest.raises(ValueError):
        await pm.update_task_status("1.1", "complete")
    # Valid transition.
    t = await pm.update_task_status("1.1", "in_progress")
    assert t.status == "in_progress"


@pytest.mark.asyncio
async def test_update_task_status_unknown_task_raises(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    with pytest.raises(PlanConcurrentModificationError):
        await pm.update_task_status("bogus", "in_progress")


@pytest.mark.asyncio
async def test_get_task_and_next_pending(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    t = await pm.get_task("1.1")
    assert t is not None and t.id == "1.1"
    nxt = await pm.next_pending_task()
    assert nxt is not None and nxt.id == "1.1"
    # Move 1.1 out of pending; next should be 1.2.
    await pm.update_task_status("1.1", "in_progress")
    nxt2 = await pm.next_pending_task()
    assert nxt2 is not None and nxt2.id == "1.2"


@pytest.mark.asyncio
async def test_retry_counting(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    await pm.update_task_status("1.1", "in_progress")
    assert await pm.mark_task_retry("1.1") == 1
    assert await pm.mark_task_retry("1.1") == 2
    t = await pm.get_task("1.1")
    assert t is not None and t.retry_count == 2


@pytest.mark.asyncio
async def test_mark_escalated_flag(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    await pm.update_task_status("1.1", "in_progress")
    await pm.mark_escalated("1.1")
    t = await pm.get_task("1.1")
    assert t is not None and t.escalated is True


@pytest.mark.asyncio
async def test_concurrent_writers_serialized(tmp_path: Path) -> None:
    """Two asyncio tasks hammering update_task_status must not corrupt the ledger."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    await pm.update_task_status("1.1", "in_progress")
    await pm.update_task_status("1.2", "in_progress")

    async def bump(task_id: str, n: int) -> None:
        for _ in range(n):
            await pm.mark_task_retry(task_id)

    await asyncio.gather(bump("1.1", 3), bump("1.2", 3))

    t1 = await pm.get_task("1.1")
    t2 = await pm.get_task("1.2")
    assert t1 is not None and t1.retry_count == 3
    assert t2 is not None and t2.retry_count == 3


@pytest.mark.asyncio
async def test_snapshot_fast_path_matches_replay(tmp_path: Path) -> None:
    """After a save, load returns the snapshotted plan without replaying from scratch."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    await pm.update_task_status("1.1", "in_progress")
    await pm.update_task_status("1.1", "coded")
    await pm.update_task_status("1.1", "auto_gated")
    # Force a fresh PlanManager instance and make sure load works.
    pm2 = PlanManager(tmp_path, session_id="s2")
    loaded = await pm2.load()
    assert loaded is not None
    assert loaded.phases[0].tasks[0].status == "auto_gated"


# ---------------------------------------------------------------------------
# Extended coverage tests — ledger_append, mark_blocked/complete, read helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ledger_append_writes_entry(tmp_path: Path) -> None:
    """ledger_append should grow the ledger file."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())

    lp = ledger_path(tmp_path)
    lines_before = len(lp.read_text().strip().splitlines())

    await pm.ledger_append(
        op="plan_tournament_complete",
        payload={"tournament_id": "t-42"},
    )

    lines_after = len(lp.read_text().strip().splitlines())
    assert lines_after == lines_before + 1


@pytest.mark.asyncio
async def test_mark_task_blocked(tmp_path: Path) -> None:
    """Mark a task as blocked via update_task_status with meta."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    await pm.update_task_status("1.1", "in_progress")
    t = await pm.update_task_status(
        "1.1",
        "blocked",
        meta={"blocked_reason": "missing API key"},
    )
    assert t.status == "blocked"
    assert t.blocked_reason == "missing API key"

    # Verify persistence via a fresh PlanManager.
    pm2 = PlanManager(tmp_path, session_id="s2")
    loaded = await pm2.load()
    assert loaded is not None
    task = loaded.phases[0].tasks[0]
    assert task.status == "blocked"
    assert task.blocked_reason == "missing API key"


@pytest.mark.asyncio
async def test_mark_task_complete_via_manager(tmp_path: Path) -> None:
    """Walk a task through the FSM to complete."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    await pm.update_task_status("1.1", "in_progress")
    await pm.update_task_status("1.1", "coded")
    await pm.update_task_status("1.1", "auto_gated")
    await pm.update_task_status("1.1", "reviewed")
    await pm.update_task_status("1.1", "tested")
    await pm.update_task_status("1.1", "tournamented")
    t = await pm.update_task_status("1.1", "complete")
    assert t.status == "complete"


def test_read_plan_json_missing(tmp_path: Path) -> None:
    """read_plan_json returns None when no plan.json exists."""
    assert read_plan_json(tmp_path) is None


def test_read_plan_json_invalid(tmp_path: Path) -> None:
    """read_plan_json returns None for invalid JSON."""
    pp = plan_path(tmp_path)
    pp.parent.mkdir(parents=True, exist_ok=True)
    pp.write_text("not valid json {{{{", encoding="utf-8")
    assert read_plan_json(tmp_path) is None


def test_current_plan_path_helper(tmp_path: Path) -> None:
    """current_plan_path should return the expected path."""
    result = current_plan_path(tmp_path)
    assert result == plan_path(tmp_path)
    assert str(result).endswith(".autodev/plan.json")


@pytest.mark.asyncio
async def test_load_rejects_unknown_ops(tmp_path: Path) -> None:
    """An entry with an unknown op fails schema validation (LedgerOp is strict)."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())

    # Manually forge an entry with an unknown op after the snapshot.
    lp = ledger_path(tmp_path)
    lines = lp.read_text().strip().splitlines()
    last = json.loads(lines[-1])
    forged: dict = {
        "seq": last["seq"] + 1,
        "timestamp": _iso(),
        "session_id": "s1",
        "op": "future_op_v99",
        "payload": {},
        "prev_hash": last["self_hash"],
    }
    from state.ledger import compute_hash

    forged["self_hash"] = compute_hash(forged)
    with lp.open("a") as fh:
        fh.write(json.dumps(forged, sort_keys=True) + "\n")

    # LedgerOp is a strict Literal — unknown ops fail schema validation.
    from errors import LedgerCorruptError

    pm2 = PlanManager(tmp_path, session_id="s2")
    with pytest.raises(LedgerCorruptError, match="failed schema validation"):
        await pm2.load()


# ---------------------------------------------------------------------------
# Property accessors and edge-case paths for full coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_properties_cwd_and_session_id(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s-props")
    assert pm.cwd == tmp_path
    assert pm.session_id == "s-props"


@pytest.mark.asyncio
async def test_get_task_returns_none_when_no_plan(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    assert await pm.get_task("1.1") is None


@pytest.mark.asyncio
async def test_next_pending_returns_none_when_no_plan(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    assert await pm.next_pending_task() is None


@pytest.mark.asyncio
async def test_update_task_status_no_plan_raises(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    with pytest.raises(PlanConcurrentModificationError, match="no plan"):
        await pm.update_task_status("1.1", "in_progress")


@pytest.mark.asyncio
async def test_update_task_with_meta_fields(tmp_path: Path) -> None:
    """update_task_status with retry_count, escalated, evidence_bundle meta."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    await pm.update_task_status("1.1", "in_progress")
    t = await pm.update_task_status(
        "1.1",
        "coded",
        meta={
            "retry_count": 2,
            "escalated": True,
            "evidence_bundle": "/ev/1.1.json",
        },
    )
    assert t.retry_count == 2
    assert t.escalated is True
    assert t.evidence_bundle == "/ev/1.1.json"


@pytest.mark.asyncio
async def test_mark_task_retry_no_plan_raises(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    with pytest.raises(PlanConcurrentModificationError, match="no plan"):
        await pm.mark_task_retry("1.1")


@pytest.mark.asyncio
async def test_mark_task_retry_unknown_task_raises(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    with pytest.raises(PlanConcurrentModificationError, match="unknown task"):
        await pm.mark_task_retry("bogus")


@pytest.mark.asyncio
async def test_mark_escalated_no_plan_raises(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    with pytest.raises(PlanConcurrentModificationError, match="no plan"):
        await pm.mark_escalated("1.1")


@pytest.mark.asyncio
async def test_mark_escalated_unknown_task_raises(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    with pytest.raises(PlanConcurrentModificationError, match="unknown task"):
        await pm.mark_escalated("bogus")


@pytest.mark.asyncio
async def test_load_fast_path_applies_post_snapshot_entries(tmp_path: Path) -> None:
    """When entries exist after the last snapshot, _apply_for_load handles them."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    # init_plan appends init_plan + snapshot. Now append entries after snapshot.
    await pm.update_task_status("1.1", "in_progress")

    # Append a mark_blocked directly via ledger to test _apply_for_load branch.
    from state.ledger import append_entry
    from state.lockfile import plan_lock as _lock

    async with _lock(tmp_path):
        await append_entry(
            tmp_path,
            op="mark_blocked",
            payload={"task_id": "1.2", "reason": "dep missing"},
            session_id="s1",
        )
    async with _lock(tmp_path):
        await append_entry(
            tmp_path,
            op="mark_complete",
            payload={"task_id": "1.1"},
            session_id="s1",
        )
    async with _lock(tmp_path):
        await append_entry(
            tmp_path,
            op="append_evidence",
            payload={"task_id": "1.1", "path": "/ev/1.1.json"},
            session_id="s1",
        )
    async with _lock(tmp_path):
        await append_entry(
            tmp_path,
            op="plan_tournament_complete",
            payload={"tournament_id": "t1"},
            session_id="s1",
        )

    # Load should apply all post-snapshot ops correctly.
    pm2 = PlanManager(tmp_path, session_id="s2")
    loaded = await pm2.load()
    assert loaded is not None
    t1 = loaded.phases[0].tasks[0]
    t2 = loaded.phases[0].tasks[1]
    assert t1.status == "complete"
    assert t1.evidence_bundle == "/ev/1.1.json"
    assert t2.status == "blocked"
    assert t2.blocked_reason == "dep missing"


# ---------------------------------------------------------------------------
# Plan.complexity — architect-emitted plan-complexity bucket
# ---------------------------------------------------------------------------


def test_plan_complexity_default_none() -> None:
    """A freshly-constructed Plan has ``complexity = None`` by default —
    the parser sets this only when an architect emits ``COMPLEXITY: <bucket>``.
    Legacy plans (no COMPLEXITY: line) keep the field as ``None`` and the
    effort resolver falls back to the user-global default downstream.
    """
    plan = _mk_plan()
    assert plan.complexity is None


def test_plan_complexity_round_trip_through_json() -> None:
    """Round-trip ``complexity="complex"`` through ``model_dump(mode="json")``
    and ``model_validate`` — the field must persist across serialization.
    """
    plan = Plan(
        plan_id="p-rt",
        spec_hash="cafebabe",
        phases=[
            Phase(
                id="1",
                title="Setup",
                tasks=[
                    Task(id="1.1", phase_id="1", title="task a", description="do a"),
                ],
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
        complexity="complex",
    )
    dumped = plan.model_dump(mode="json")
    reloaded = Plan.model_validate(dumped)
    assert reloaded.complexity == "complex"
    assert reloaded == plan


def test_plan_complexity_rejects_invalid() -> None:
    """``"moderate"`` is not in the {simple, medium, complex} enum and must
    fail validation — guards against architect output drift.
    """
    with pytest.raises(ValidationError):
        Plan(
            plan_id="p-bad",
            spec_hash="cafebabe",
            phases=[
                Phase(
                    id="1",
                    title="Setup",
                    tasks=[
                        Task(id="1.1", phase_id="1", title="t", description="d"),
                    ],
                ),
            ],
            created_at=_iso(),
            updated_at=_iso(),
            complexity="moderate",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# v0.9.0 — append_corrective_tasks + update_phase_meta
# ---------------------------------------------------------------------------


def _mk_corrective_task(idx: int = 1) -> Task:
    return Task(
        id=f"1.c{idx}",
        phase_id="1",
        title=f"corrective {idx}",
        description=f"corrective body {idx}",
        complexity="medium",
        assigned_agent="developer",
        metadata={"origin": "phase_review_corrective"},
    )


@pytest.mark.asyncio
async def test_append_corrective_tasks_appends_to_phase(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    plan = await pm.append_corrective_tasks("1", [_mk_corrective_task(1)])
    phase = plan.phases[0]
    assert any(t.id == "1.c1" for t in phase.tasks)
    assert "1.c1" in phase.corrective_task_ids


@pytest.mark.asyncio
async def test_append_corrective_tasks_records_ledger_op(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    await pm.append_corrective_tasks("1", [_mk_corrective_task(1)])
    entries = await pm.read_ledger()
    ops = [e.op for e in entries]
    assert "append_corrective_tasks" in ops


@pytest.mark.asyncio
async def test_append_corrective_tasks_updates_review_status(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    plan = await pm.append_corrective_tasks(
        "1", [_mk_corrective_task(1)], review_status="corrective_required"
    )
    assert plan.phases[0].review_status == "corrective_required"


@pytest.mark.asyncio
async def test_append_corrective_tasks_idempotent_replay(tmp_path: Path) -> None:
    """Calling twice with the same ids does not create duplicates."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    await pm.append_corrective_tasks("1", [_mk_corrective_task(1)])
    await pm.append_corrective_tasks("1", [_mk_corrective_task(1)])
    plan = await pm.load()
    assert plan is not None
    matching = [t for t in plan.phases[0].tasks if t.id == "1.c1"]
    assert len(matching) == 1
    assert plan.phases[0].corrective_task_ids.count("1.c1") == 1


# ---------------------------------------------------------------------------
# WS4: append_corrective_tasks re-runs dependency_inference.infer_dependencies
# on the phase's task list right after the append succeeds, so overlapping
# corrective tasks (now that the parser populates Task.files) get an inferred
# depends_on edge — closing the gap where correctives were structurally
# invisible to overlap-avoidance.
# ---------------------------------------------------------------------------


def _mk_corrective_task_with_files(idx: int, files: list[str]) -> Task:
    return Task(
        id=f"1.c{idx}",
        phase_id="1",
        title=f"corrective {idx}",
        description=f"corrective body {idx}",
        complexity="medium",
        assigned_agent="developer",
        files=list(files),
        metadata={"origin": "phase_review_corrective"},
    )


@pytest.mark.asyncio
async def test_append_corrective_tasks_infers_deps_same_call(
    tmp_path: Path,
) -> None:
    """Two correctives appended in the SAME call, sharing a file, get an
    inferred depends_on edge."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    first = _mk_corrective_task_with_files(1, ["src/shared.py"])
    second = _mk_corrective_task_with_files(2, ["src/shared.py", "src/other.py"])
    plan = await pm.append_corrective_tasks("1", [first, second])
    tasks_by_id = {t.id: t for t in plan.phases[0].tasks}
    assert tasks_by_id["1.c2"].depends_on == ["1.c1"]
    assert tasks_by_id["1.c1"].depends_on == []


@pytest.mark.asyncio
async def test_append_corrective_tasks_infers_deps_across_separate_rounds(
    tmp_path: Path,
) -> None:
    """The realistic shape: two SEPARATE corrective rounds (e.g. two
    architect-refine cycles). The second round's task still gets wired to
    the first round's, proving inference safely re-runs on an
    already-partially-inferred task list."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    await pm.append_corrective_tasks(
        "1", [_mk_corrective_task_with_files(1, ["src/shared.py"])]
    )
    plan = await pm.append_corrective_tasks(
        "1", [_mk_corrective_task_with_files(2, ["src/shared.py"])]
    )
    tasks_by_id = {t.id: t for t in plan.phases[0].tasks}
    assert tasks_by_id["1.c2"].depends_on == ["1.c1"]


@pytest.mark.asyncio
async def test_append_corrective_tasks_reinfer_does_not_disturb_existing_deps(
    tmp_path: Path,
) -> None:
    """Re-running inference after an append must never touch a task that
    already has an explicit (or previously-inferred) depends_on."""
    pm = PlanManager(tmp_path, session_id="s1")
    plan = _mk_plan()
    plan.phases[0].tasks[1].depends_on = ["1.1"]  # pre-existing explicit edge
    await pm.init_plan(plan)
    await pm.append_corrective_tasks(
        "1", [_mk_corrective_task_with_files(1, ["src/shared.py"])]
    )
    loaded = await pm.load()
    assert loaded is not None
    tasks_by_id = {t.id: t for t in loaded.phases[0].tasks}
    assert tasks_by_id["1.2"].depends_on == ["1.1"]


@pytest.mark.asyncio
async def test_append_corrective_tasks_reinfer_survives_ledger_replay(
    tmp_path: Path,
) -> None:
    """The inferred depends_on edge is part of the SAME
    ``append_corrective_tasks`` ledger op (no separate op needed) — a fresh
    PlanManager replaying from the ledger reproduces the edge."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    first = _mk_corrective_task_with_files(1, ["src/shared.py"])
    second = _mk_corrective_task_with_files(2, ["src/shared.py"])
    await pm.append_corrective_tasks("1", [first, second])

    pm2 = PlanManager(tmp_path, session_id="s2")
    plan = await pm2.load()
    assert plan is not None
    tasks_by_id = {t.id: t for t in plan.phases[0].tasks}
    assert tasks_by_id["1.c2"].depends_on == ["1.c1"]


@pytest.mark.asyncio
async def test_update_phase_meta_persists_baseline_commit(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    plan = await pm.update_phase_meta("1", baseline_commit="deadbeef1234")
    assert plan.phases[0].baseline_commit == "deadbeef1234"


@pytest.mark.asyncio
async def test_update_phase_meta_persists_review_status(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    plan = await pm.update_phase_meta("1", review_status="accepted")
    assert plan.phases[0].review_status == "accepted"


@pytest.mark.asyncio
async def test_ledger_replay_includes_corrective_append(tmp_path: Path) -> None:
    """After append_corrective_tasks, replay (load) reproduces the state."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    await pm.append_corrective_tasks("1", [_mk_corrective_task(1)])
    # Force a full replay path: rebuild a new manager + load.
    pm2 = PlanManager(tmp_path, session_id="s2")
    plan = await pm2.load()
    assert plan is not None
    assert any(t.id == "1.c1" for t in plan.phases[0].tasks)
    assert "1.c1" in plan.phases[0].corrective_task_ids
    assert plan.phases[0].review_status == "corrective_required"


@pytest.mark.asyncio
async def test_ledger_replay_includes_phase_meta_update(tmp_path: Path) -> None:
    """After update_phase_meta, replay reproduces the metadata."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    await pm.update_phase_meta("1", baseline_commit="abc123", review_status="accepted")
    pm2 = PlanManager(tmp_path, session_id="s2")
    plan = await pm2.load()
    assert plan is not None
    assert plan.phases[0].baseline_commit == "abc123"
    assert plan.phases[0].review_status == "accepted"


@pytest.mark.asyncio
async def test_append_corrective_tasks_unknown_phase_raises(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    with pytest.raises(PlanConcurrentModificationError):
        await pm.append_corrective_tasks("999", [_mk_corrective_task(1)])


@pytest.mark.asyncio
async def test_update_phase_meta_unknown_phase_raises(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    with pytest.raises(PlanConcurrentModificationError):
        await pm.update_phase_meta("999", baseline_commit="abc")


# ---------------------------------------------------------------------------
# v0.11.0 — next_pending_tasks (DAG-aware multi-task selector)
# ---------------------------------------------------------------------------


def _mk_dag_plan() -> Plan:
    """Build a plan whose first phase has a DAG: 1.1 → 1.2 ; 1.3 (independent)."""
    return Plan(
        plan_id="p-dag",
        spec_hash="dead",
        phases=[
            Phase(
                id="1",
                title="parallel-fixture",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="root",
                        description="r",
                        files=["src/a.py"],
                    ),
                    Task(
                        id="1.2",
                        phase_id="1",
                        title="dependent",
                        description="d",
                        depends_on=["1.1"],
                        files=["src/b.py"],
                    ),
                    Task(
                        id="1.3",
                        phase_id="1",
                        title="independent",
                        description="i",
                        files=["src/c.py"],
                    ),
                ],
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
    )


@pytest.mark.asyncio
async def test_next_pending_tasks_returns_up_to_limit(tmp_path: Path) -> None:
    """limit=2 returns at most two pending tasks (1.1 and 1.3 — 1.2 deps unmet)."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_dag_plan())
    out = await pm.next_pending_tasks(limit=2)
    ids = [t.id for t in out]
    assert ids == ["1.1", "1.3"]


@pytest.mark.asyncio
async def test_next_pending_tasks_excludes_unmet_deps(tmp_path: Path) -> None:
    """1.2 (depends on 1.1) is NOT returned while 1.1 is still pending."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_dag_plan())
    out = await pm.next_pending_tasks(limit=10)
    assert "1.2" not in {t.id for t in out}


@pytest.mark.asyncio
async def test_next_pending_tasks_excludes_overlapping_files(
    tmp_path: Path,
) -> None:
    """exclude_files={'src/a.py'} hides 1.1 (whose files include src/a.py)."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_dag_plan())
    out = await pm.next_pending_tasks(limit=10, exclude_files={"src/a.py"})
    assert "1.1" not in {t.id for t in out}
    # 1.3 has files=['src/c.py'] — unaffected.
    assert "1.3" in {t.id for t in out}


@pytest.mark.asyncio
async def test_next_pending_tasks_returns_dependent_after_parent_terminal(
    tmp_path: Path,
) -> None:
    """Once 1.1 is complete, 1.2's depends_on is satisfied and it's selectable."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_dag_plan())
    # Walk 1.1 to complete via FSM.
    for s in ("in_progress", "coded", "auto_gated", "reviewed", "tested", "tournamented", "complete"):
        await pm.update_task_status("1.1", s)
    out = await pm.next_pending_tasks(limit=10)
    assert "1.2" in {t.id for t in out}


@pytest.mark.asyncio
async def test_next_pending_tasks_treats_blocked_dep_as_terminal(
    tmp_path: Path,
) -> None:
    """A blocked parent satisfies the depends_on (terminal) — child becomes selectable."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_dag_plan())
    # Move 1.1 to in_progress then to blocked.
    await pm.update_task_status("1.1", "in_progress")
    await pm.update_task_status(
        "1.1", "blocked", meta={"blocked_reason": "test"}
    )
    out = await pm.next_pending_tasks(limit=10)
    assert "1.2" in {t.id for t in out}


@pytest.mark.asyncio
async def test_next_pending_tasks_empty_when_no_pending(tmp_path: Path) -> None:
    """No pending tasks → empty list (not None)."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_dag_plan())
    # Block all three.
    for tid in ("1.1", "1.2", "1.3"):
        await pm.update_task_status(tid, "in_progress")
        await pm.update_task_status(
            tid, "blocked", meta={"blocked_reason": "x"}
        )
    out = await pm.next_pending_tasks(limit=10)
    assert out == []


@pytest.mark.asyncio
async def test_next_pending_tasks_limit_zero_normalized_to_one(
    tmp_path: Path,
) -> None:
    """limit=0 is normalized to 1 (defensive — dispatcher should clamp first)."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_dag_plan())
    out = await pm.next_pending_tasks(limit=0)
    assert len(out) == 1


@pytest.mark.asyncio
async def test_next_pending_task_legacy_shim_returns_first(
    tmp_path: Path,
) -> None:
    """next_pending_task() returns the first task that next_pending_tasks would."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_dag_plan())
    one = await pm.next_pending_task()
    assert one is not None and one.id == "1.1"


# ---------------------------------------------------------------------------
# v0.11.0 — in-flight tracking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_in_flight_appends_ledger_op(tmp_path: Path) -> None:
    """mark_in_flight writes a ``mark_in_flight`` ledger entry."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_dag_plan())
    lp = ledger_path(tmp_path)
    before = len(lp.read_text().strip().splitlines())
    await pm.mark_in_flight("1.1")
    after = len(lp.read_text().strip().splitlines())
    assert after == before + 1


@pytest.mark.asyncio
async def test_clear_in_flight_appends_ledger_op(tmp_path: Path) -> None:
    """clear_in_flight writes a ``clear_in_flight`` ledger entry."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_dag_plan())
    await pm.mark_in_flight("1.1")
    lp = ledger_path(tmp_path)
    before = len(lp.read_text().strip().splitlines())
    await pm.clear_in_flight("1.1")
    after = len(lp.read_text().strip().splitlines())
    assert after == before + 1


@pytest.mark.asyncio
async def test_phase_in_flight_count_zero_initially(tmp_path: Path) -> None:
    """No tasks are in-flight before mark_in_flight runs."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_dag_plan())
    assert await pm.phase_in_flight_count("1") == 0


@pytest.mark.asyncio
async def test_phase_in_flight_count_after_mark(tmp_path: Path) -> None:
    """phase_in_flight_count reflects current in-memory in-flight set."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_dag_plan())
    await pm.mark_in_flight("1.1")
    await pm.mark_in_flight("1.3")
    assert await pm.phase_in_flight_count("1") == 2
    await pm.clear_in_flight("1.1")
    assert await pm.phase_in_flight_count("1") == 1


@pytest.mark.asyncio
async def test_in_flight_files_unions_correctly(tmp_path: Path) -> None:
    """in_flight_files returns the union of in-flight tasks' Task.files."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_dag_plan())
    await pm.mark_in_flight("1.1")  # files=src/a.py
    await pm.mark_in_flight("1.3")  # files=src/c.py
    files = await pm.in_flight_files()
    assert files == {"src/a.py", "src/c.py"}


@pytest.mark.asyncio
async def test_in_flight_set_not_persisted_across_resume(
    tmp_path: Path,
) -> None:
    """A fresh PlanManager rebuilds an empty in_flight set even if
    the ledger contains mark_in_flight entries (in-memory only)."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_dag_plan())
    await pm.mark_in_flight("1.1")
    await pm.mark_in_flight("1.3")
    assert await pm.phase_in_flight_count("1") == 2

    # Construct a fresh manager (simulating resume).
    pm2 = PlanManager(tmp_path, session_id="s2")
    # Load works — ledger ops mark_in_flight were no-ops on plan state.
    plan = await pm2.load()
    assert plan is not None
    # And the new manager observes zero in-flight.
    assert await pm2.phase_in_flight_count("1") == 0
    assert await pm2.in_flight_files() == set()


# ---------------------------------------------------------------------------
# v0.11.0 — mark_blocked_descendants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_blocked_descendants_marks_chain(tmp_path: Path) -> None:
    """Failing 1.1 cascade-blocks 1.2 with structured blocked_reason."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_dag_plan())
    blocked = await pm.mark_blocked_descendants(
        phase_id="1", failed_task_id="1.1", reason="adapter failure"
    )
    assert blocked == ["1.2"]
    t = await pm.get_task("1.2")
    assert t is not None and t.status == "blocked"
    assert t.blocked_reason is not None
    assert "upstream-failure:1.1:adapter failure" in t.blocked_reason


@pytest.mark.asyncio
async def test_mark_blocked_descendants_does_not_block_independent(
    tmp_path: Path,
) -> None:
    """Failing 1.1 does NOT block 1.3 (independent — no depends_on)."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_dag_plan())
    await pm.mark_blocked_descendants("1", "1.1", "x")
    t = await pm.get_task("1.3")
    assert t is not None and t.status == "pending"


@pytest.mark.asyncio
async def test_mark_blocked_descendants_skips_already_terminal(
    tmp_path: Path,
) -> None:
    """An already-complete descendant is not re-blocked by the cascade."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_dag_plan())
    # Walk 1.2 to complete artificially via FSM (depends_on doesn't gate FSM).
    for s in ("in_progress", "coded", "auto_gated", "reviewed", "tested", "tournamented", "complete"):
        await pm.update_task_status("1.2", s)
    blocked = await pm.mark_blocked_descendants("1", "1.1", "x")
    assert blocked == []
    t = await pm.get_task("1.2")
    assert t is not None and t.status == "complete"


@pytest.mark.asyncio
async def test_mark_blocked_descendants_persists_across_resume(
    tmp_path: Path,
) -> None:
    """The cascade persists via the ledger and survives a fresh PlanManager."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_dag_plan())
    await pm.mark_blocked_descendants("1", "1.1", "test")
    pm2 = PlanManager(tmp_path, session_id="s2")
    t = await pm2.get_task("1.2")
    assert t is not None and t.status == "blocked"
    assert t.blocked_reason and "upstream-failure:1.1:test" in t.blocked_reason
