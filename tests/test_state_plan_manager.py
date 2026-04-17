"""Tests for :mod:`src.state.plan_manager`."""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
from pathlib import Path

import pytest

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
