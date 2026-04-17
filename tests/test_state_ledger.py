"""Tests for :mod:`src.state.ledger`."""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
from pathlib import Path

import pytest

from errors import LedgerCorruptError
from state.ledger import (
    append_entry,
    compute_hash,
    read_entries,
    replay_ledger,
    snapshot_plan,
)
from state.lockfile import plan_lock
from state.paths import ledger_path
from state.schemas import Phase, Plan, Task


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan() -> Plan:
    return Plan(
        plan_id="p-test",
        spec_hash="deadbeef",
        phases=[
            Phase(
                id="1",
                title="Setup",
                tasks=[
                    Task(id="1.1", phase_id="1", title="a", description="aa"),
                    Task(id="1.2", phase_id="1", title="b", description="bb"),
                ],
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
    )


@pytest.mark.asyncio
async def test_genesis_entry_has_empty_prev_hash(tmp_path: Path) -> None:
    plan = _mk_plan()
    async with plan_lock(tmp_path):
        entry = await append_entry(
            tmp_path,
            op="init_plan",
            payload={"plan": plan.model_dump(mode="json")},
            session_id="s1",
        )
    assert entry.seq == 1
    assert entry.prev_hash == ""
    assert entry.self_hash  # non-empty


@pytest.mark.asyncio
async def test_hash_chain_links_entries(tmp_path: Path) -> None:
    plan = _mk_plan()
    async with plan_lock(tmp_path):
        e1 = await append_entry(
            tmp_path,
            op="init_plan",
            payload={"plan": plan.model_dump(mode="json")},
            session_id="s1",
        )
    async with plan_lock(tmp_path):
        e2 = await append_entry(
            tmp_path,
            op="update_task_status",
            payload={"task_id": "1.1", "status": "in_progress"},
            session_id="s1",
        )
    assert e2.prev_hash == e1.self_hash
    assert e2.seq == 2


@pytest.mark.asyncio
async def test_tampered_middle_entry_detected(tmp_path: Path) -> None:
    plan = _mk_plan()
    async with plan_lock(tmp_path):
        await append_entry(
            tmp_path,
            op="init_plan",
            payload={"plan": plan.model_dump(mode="json")},
            session_id="s1",
        )
    async with plan_lock(tmp_path):
        await append_entry(
            tmp_path,
            op="update_task_status",
            payload={"task_id": "1.1", "status": "in_progress"},
            session_id="s1",
        )
    async with plan_lock(tmp_path):
        await append_entry(
            tmp_path,
            op="update_task_status",
            payload={"task_id": "1.1", "status": "coded"},
            session_id="s1",
        )

    # Tamper line 2's payload and rewrite the file.
    lp = ledger_path(tmp_path)
    lines = lp.read_text().strip().splitlines()
    doc = json.loads(lines[1])
    doc["payload"]["status"] = "complete"  # changed without updating self_hash
    lines[1] = json.dumps(doc, sort_keys=True)
    lp.write_text("\n".join(lines) + "\n")

    with pytest.raises(LedgerCorruptError):
        read_entries(tmp_path)


@pytest.mark.asyncio
async def test_concurrent_appends_serialized_under_lock(tmp_path: Path) -> None:
    """Two concurrent writers must produce a valid hash chain, not overwrite."""
    plan = _mk_plan()
    async with plan_lock(tmp_path):
        await append_entry(
            tmp_path,
            op="init_plan",
            payload={"plan": plan.model_dump(mode="json")},
            session_id="s1",
        )

    async def writer(sid: str, status: str) -> None:
        async with plan_lock(tmp_path):
            await append_entry(
                tmp_path,
                op="update_task_status",
                payload={"task_id": "1.1", "status": status},
                session_id=sid,
            )

    await asyncio.gather(
        writer("w-a", "in_progress"),
        writer("w-b", "coded"),
        writer("w-c", "reviewed"),
    )

    entries = read_entries(tmp_path)
    assert [e.seq for e in entries] == [1, 2, 3, 4]
    # Chain intact.
    for i in range(1, len(entries)):
        assert entries[i].prev_hash == entries[i - 1].self_hash


@pytest.mark.asyncio
async def test_replay_reconstructs_plan_from_ops(tmp_path: Path) -> None:
    plan = _mk_plan()
    async with plan_lock(tmp_path):
        await append_entry(
            tmp_path,
            op="init_plan",
            payload={"plan": plan.model_dump(mode="json")},
            session_id="s1",
        )
    async with plan_lock(tmp_path):
        await append_entry(
            tmp_path,
            op="update_task_status",
            payload={"task_id": "1.1", "status": "in_progress"},
            session_id="s1",
        )
    async with plan_lock(tmp_path):
        await append_entry(
            tmp_path,
            op="update_task_status",
            payload={"task_id": "1.1", "status": "complete"},
            session_id="s1",
        )

    out, entries = replay_ledger(tmp_path)
    assert out is not None
    assert len(entries) == 3
    task = out.phases[0].tasks[0]
    assert task.id == "1.1"
    assert task.status == "complete"


@pytest.mark.asyncio
async def test_truncated_partial_line_detected(tmp_path: Path) -> None:
    """Simulate kill -9 mid-append: trailing partial JSON line."""
    plan = _mk_plan()
    async with plan_lock(tmp_path):
        await append_entry(
            tmp_path,
            op="init_plan",
            payload={"plan": plan.model_dump(mode="json")},
            session_id="s1",
        )

    # Manually corrupt by appending a partial line.
    lp = ledger_path(tmp_path)
    with lp.open("a") as fh:
        fh.write('{"seq": 2, "op": "update_task_')
    # read_entries should raise with a helpful message mentioning recovery.
    with pytest.raises(LedgerCorruptError) as excinfo:
        read_entries(tmp_path)
    assert "valid JSON" in str(excinfo.value) or "line" in str(excinfo.value)


@pytest.mark.asyncio
async def test_snapshot_writes_plan_json_and_entry(tmp_path: Path) -> None:
    plan = _mk_plan()
    async with plan_lock(tmp_path):
        await append_entry(
            tmp_path,
            op="init_plan",
            payload={"plan": plan.model_dump(mode="json")},
            session_id="s1",
        )
    async with plan_lock(tmp_path):
        entry = await snapshot_plan(tmp_path, plan, session_id="s1")
    assert entry.op == "snapshot"
    pp = tmp_path / ".autodev" / "plan.json"
    assert pp.exists()
    parsed = json.loads(pp.read_text())
    assert parsed["plan_id"] == plan.plan_id


def test_compute_hash_is_deterministic() -> None:
    a = {"seq": 1, "op": "x", "payload": {"b": 1, "a": 2}}
    b = {"op": "x", "seq": 1, "payload": {"a": 2, "b": 1}}  # same, different key order
    assert compute_hash(a) == compute_hash(b)


@pytest.mark.asyncio
async def test_read_entries_nonexistent_returns_empty(tmp_path: Path) -> None:
    assert read_entries(tmp_path) == []
    out, entries = replay_ledger(tmp_path)
    assert out is None
    assert entries == []


# ---------------------------------------------------------------------------
# Extended coverage tests — _apply_op branches, _read_tail edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_mark_blocked(tmp_path: Path) -> None:
    """Append a mark_blocked op and verify task.status == 'blocked'."""
    plan = _mk_plan()
    async with plan_lock(tmp_path):
        await append_entry(
            tmp_path,
            op="init_plan",
            payload={"plan": plan.model_dump(mode="json")},
            session_id="s1",
        )
    async with plan_lock(tmp_path):
        await append_entry(
            tmp_path,
            op="mark_blocked",
            payload={"task_id": "1.1", "reason": "waiting for dep"},
            session_id="s1",
        )

    out, entries = replay_ledger(tmp_path)
    assert out is not None
    task = out.phases[0].tasks[0]
    assert task.status == "blocked"
    assert task.blocked_reason == "waiting for dep"


@pytest.mark.asyncio
async def test_replay_mark_complete(tmp_path: Path) -> None:
    """Append a mark_complete op and verify task.status == 'complete'."""
    plan = _mk_plan()
    async with plan_lock(tmp_path):
        await append_entry(
            tmp_path,
            op="init_plan",
            payload={"plan": plan.model_dump(mode="json")},
            session_id="s1",
        )
    async with plan_lock(tmp_path):
        await append_entry(
            tmp_path,
            op="mark_complete",
            payload={"task_id": "1.2"},
            session_id="s1",
        )

    out, _ = replay_ledger(tmp_path)
    assert out is not None
    task = out.phases[0].tasks[1]
    assert task.id == "1.2"
    assert task.status == "complete"


@pytest.mark.asyncio
async def test_replay_append_evidence(tmp_path: Path) -> None:
    """Append an append_evidence op and verify task.evidence_bundle."""
    plan = _mk_plan()
    async with plan_lock(tmp_path):
        await append_entry(
            tmp_path,
            op="init_plan",
            payload={"plan": plan.model_dump(mode="json")},
            session_id="s1",
        )
    async with plan_lock(tmp_path):
        await append_entry(
            tmp_path,
            op="append_evidence",
            payload={"task_id": "1.1", "path": ".autodev/evidence/1.1-developer.json"},
            session_id="s1",
        )

    out, _ = replay_ledger(tmp_path)
    assert out is not None
    task = out.phases[0].tasks[0]
    assert task.evidence_bundle == ".autodev/evidence/1.1-developer.json"


@pytest.mark.asyncio
async def test_replay_unknown_op_raises(tmp_path: Path) -> None:
    """An unknown op in the ledger should raise LedgerCorruptError.

    The LedgerEntry schema enforces a Literal type on ``op``, so an
    unrecognised op triggers a schema validation error during read_entries
    (before _apply_op is even reached).
    """
    plan = _mk_plan()
    async with plan_lock(tmp_path):
        await append_entry(
            tmp_path,
            op="init_plan",
            payload={"plan": plan.model_dump(mode="json")},
            session_id="s1",
        )

    # Manually forge a ledger line with an unknown op.
    lp = ledger_path(tmp_path)
    lines = lp.read_text().strip().splitlines()
    last = json.loads(lines[-1])
    forged: dict = {
        "seq": last["seq"] + 1,
        "timestamp": _iso(),
        "session_id": "s1",
        "op": "totally_unknown",
        "payload": {},
        "prev_hash": last["self_hash"],
    }
    forged["self_hash"] = compute_hash(forged)
    with lp.open("a") as fh:
        fh.write(json.dumps(forged, sort_keys=True) + "\n")

    with pytest.raises(LedgerCorruptError, match="schema validation"):
        replay_ledger(tmp_path)


@pytest.mark.asyncio
async def test_replay_update_task_with_all_metadata(tmp_path: Path) -> None:
    """update_task_status with blocked_reason, retry_count, escalated, evidence_bundle."""
    plan = _mk_plan()
    async with plan_lock(tmp_path):
        await append_entry(
            tmp_path,
            op="init_plan",
            payload={"plan": plan.model_dump(mode="json")},
            session_id="s1",
        )
    async with plan_lock(tmp_path):
        await append_entry(
            tmp_path,
            op="update_task_status",
            payload={
                "task_id": "1.1",
                "status": "in_progress",
                "blocked_reason": "api timeout",
                "retry_count": 3,
                "escalated": True,
                "evidence_bundle": "/evidence/1.1.json",
            },
            session_id="s1",
        )

    out, _ = replay_ledger(tmp_path)
    assert out is not None
    task = out.phases[0].tasks[0]
    assert task.status == "in_progress"
    assert task.blocked_reason == "api timeout"
    assert task.retry_count == 3
    assert task.escalated is True
    assert task.evidence_bundle == "/evidence/1.1.json"


@pytest.mark.asyncio
async def test_plan_tournament_complete_is_noop(tmp_path: Path) -> None:
    """plan_tournament_complete should not mutate the plan."""
    plan = _mk_plan()
    async with plan_lock(tmp_path):
        await append_entry(
            tmp_path,
            op="plan_tournament_complete",
            payload={"tournament_id": "t1"},
            session_id="s1",
        )
    async with plan_lock(tmp_path):
        await append_entry(
            tmp_path,
            op="init_plan",
            payload={"plan": plan.model_dump(mode="json")},
            session_id="s1",
        )

    out, entries = replay_ledger(tmp_path)
    assert out is not None
    assert len(entries) == 2
    # Plan should be exactly as initialized — tournament op did not mutate it.
    assert out.plan_id == "p-test"
    assert out.phases[0].tasks[0].status == "pending"


@pytest.mark.asyncio
async def test_impl_tournament_complete_is_noop(tmp_path: Path) -> None:
    """impl_tournament_complete should not mutate the plan."""
    plan = _mk_plan()
    async with plan_lock(tmp_path):
        await append_entry(
            tmp_path,
            op="init_plan",
            payload={"plan": plan.model_dump(mode="json")},
            session_id="s1",
        )
    async with plan_lock(tmp_path):
        await append_entry(
            tmp_path,
            op="impl_tournament_complete",
            payload={"tournament_id": "t2"},
            session_id="s1",
        )

    out, entries = replay_ledger(tmp_path)
    assert out is not None
    assert len(entries) == 2
    assert out.phases[0].tasks[0].status == "pending"


@pytest.mark.asyncio
async def test_corrupt_last_line_raises(tmp_path: Path) -> None:
    """Invalid JSON as the last line triggers LedgerCorruptError on next append."""
    plan = _mk_plan()
    async with plan_lock(tmp_path):
        await append_entry(
            tmp_path,
            op="init_plan",
            payload={"plan": plan.model_dump(mode="json")},
            session_id="s1",
        )

    # Corrupt the file by appending a complete but non-JSON line.
    lp = ledger_path(tmp_path)
    with lp.open("a") as fh:
        fh.write("this is not json at all\n")

    with pytest.raises(LedgerCorruptError, match="not valid JSON"):
        async with plan_lock(tmp_path):
            await append_entry(
                tmp_path,
                op="update_task_status",
                payload={"task_id": "1.1", "status": "in_progress"},
                session_id="s1",
            )


@pytest.mark.asyncio
async def test_missing_seq_hash_fields_raises(tmp_path: Path) -> None:
    """Valid JSON without seq/self_hash fields triggers LedgerCorruptError."""
    plan = _mk_plan()
    async with plan_lock(tmp_path):
        await append_entry(
            tmp_path,
            op="init_plan",
            payload={"plan": plan.model_dump(mode="json")},
            session_id="s1",
        )

    # Write valid JSON that lacks required fields as the last line.
    lp = ledger_path(tmp_path)
    with lp.open("a") as fh:
        fh.write(json.dumps({"op": "noop", "payload": {}}) + "\n")

    with pytest.raises(LedgerCorruptError, match="missing seq/self_hash"):
        async with plan_lock(tmp_path):
            await append_entry(
                tmp_path,
                op="update_task_status",
                payload={"task_id": "1.1", "status": "in_progress"},
                session_id="s1",
            )
