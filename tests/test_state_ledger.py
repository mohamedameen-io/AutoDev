"""Tests for :mod:`src.state.ledger`."""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import tempfile
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

hypothesis = pytest.importorskip("hypothesis")
given = hypothesis.given
settings = hypothesis.settings
st = hypothesis.strategies


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


@given(st.integers(min_value=1, max_value=30))
@settings(max_examples=20, deadline=None)
def test_hash_chain_property_holds_across_many_appends(num_updates: int) -> None:
    """Property-style guard: seq monotonicity and prev_hash links always hold."""

    async def _run(tmp_path: Path) -> None:
        plan = _mk_plan()
        async with plan_lock(tmp_path):
            await append_entry(
                tmp_path,
                op="init_plan",
                payload={"plan": plan.model_dump(mode="json")},
                session_id="s1",
            )

        statuses = ["in_progress", "coded", "reviewed", "complete"]
        for i in range(num_updates):
            async with plan_lock(tmp_path):
                await append_entry(
                    tmp_path,
                    op="update_task_status",
                    payload={"task_id": "1.1", "status": statuses[i % len(statuses)]},
                    session_id=f"w{i}",
                )

        entries = read_entries(tmp_path)
        assert len(entries) == num_updates + 1
        assert [e.seq for e in entries] == list(range(1, num_updates + 2))
        for i in range(1, len(entries)):
            assert entries[i].prev_hash == entries[i - 1].self_hash

    with tempfile.TemporaryDirectory() as td:
        asyncio.run(_run(Path(td)))


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


# ---------------------------------------------------------------------------
# v0.12.0 — multi-branch ledger ops are audit-only (no plan mutation)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_branch_ledger_ops_replay_correctly(tmp_path: Path) -> None:
    """All three v0.12.0 multi-branch ops are audit-only.

    ``multi_branch_plan_tournament_start``,
    ``multi_branch_meta_merge_complete``, and
    ``multi_branch_plan_tournament_complete`` MUST NOT mutate plan
    state during replay — they are forensics breadcrumbs only. The
    per-branch ``plan_tournament_complete`` ops carry the per-branch
    state; these aggregate three ops just record 'a multi-branch run
    happened with these survivors and final hash'.
    """
    plan = _mk_plan()
    # Append the three multi-branch ops BEFORE init_plan to test the
    # replay path that survives the "no plan yet" branch.
    async with plan_lock(tmp_path):
        await append_entry(
            tmp_path,
            op="multi_branch_plan_tournament_start",
            payload={
                "spec_hash": "deadbeef00000000",
                "n_branches": 3,
                "branch_seeds": ["100", "101", "102"],
            },
            session_id="s1",
        )
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
            op="multi_branch_meta_merge_complete",
            payload={
                "spec_hash": "deadbeef00000000",
                "n_survivors": 3,
                "n_steps": 2,
                "meta_passes": 2,
            },
            session_id="s1",
        )
    async with plan_lock(tmp_path):
        await append_entry(
            tmp_path,
            op="multi_branch_plan_tournament_complete",
            payload={
                "spec_hash": "deadbeef00000000",
                "n_branches": 3,
                "n_survivors": 3,
                "final_hash": "abcdef0123456789",
            },
            session_id="s1",
        )

    out, entries = replay_ledger(tmp_path)
    assert out is not None
    assert len(entries) == 4
    # Plan state is unchanged from init_plan — the multi-branch ops did
    # not mutate any task/phase fields.
    assert out.plan_id == "p-test"
    assert out.phases[0].tasks[0].status == "pending"
    assert out.phases[0].tasks[1].status == "pending"


@pytest.mark.asyncio
async def test_multi_branch_start_op_before_init_plan_is_safe(
    tmp_path: Path,
) -> None:
    """``multi_branch_plan_tournament_start`` may legitimately appear
    BEFORE ``init_plan`` (the plan is built FROM the multi-branch
    tournament's output). Replay must tolerate this ordering — same
    invariant as ``plan_tournament_complete``."""
    plan = _mk_plan()
    async with plan_lock(tmp_path):
        await append_entry(
            tmp_path,
            op="multi_branch_plan_tournament_start",
            payload={
                "spec_hash": "deadbeef00000000",
                "n_branches": 3,
                "branch_seeds": ["1", "2", "3"],
            },
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
    # Replay completed without raising — pre-init_plan multi-branch op
    # is treated as a no-op like plan_tournament_complete.
    assert out is not None
    assert len(entries) == 2


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


# ---------------------------------------------------------------------------
# v0.15.0 — stuck-ladder + course-correction ledger ops are audit-only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stuck_refine_op_replays_correctly(tmp_path: Path) -> None:
    """``stuck_refine`` is audit-only — does NOT mutate plan state."""
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
            op="stuck_refine",
            payload={
                "task_id": "1.1",
                "reason": "reviewer NEEDS_CHANGES x3",
                "critic_response_excerpt": "add type hints to signature",
            },
            session_id="s1",
        )

    out, entries = replay_ledger(tmp_path)
    assert out is not None
    assert len(entries) == 2
    assert out.phases[0].tasks[0].status == "pending"  # not mutated


@pytest.mark.asyncio
async def test_stuck_pivot_op_replays_correctly(tmp_path: Path) -> None:
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
            op="stuck_pivot",
            payload={
                "task_id": "1.1",
                "reason": "5 discards on same approach",
                "critic_response_excerpt": "stop using shell=True; use argv list",
            },
            session_id="s1",
        )

    out, entries = replay_ledger(tmp_path)
    assert out is not None
    assert len(entries) == 2
    assert out.phases[0].tasks[0].status == "pending"


@pytest.mark.asyncio
async def test_soft_blocker_handoff_op_replays_correctly(tmp_path: Path) -> None:
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
            op="soft_blocker_handoff",
            payload={
                "task_id": "1.1",
                "reason": "3 pivots failed; human decision required",
                "critic_response_excerpt": "decide hardware target family",
            },
            session_id="s1",
        )

    out, entries = replay_ledger(tmp_path)
    assert out is not None
    assert len(entries) == 2
    assert out.phases[0].tasks[0].status == "pending"


@pytest.mark.asyncio
async def test_drift_verifier_complete_op_replays_correctly(tmp_path: Path) -> None:
    """``drift_verifier_complete`` is audit-only — does NOT mutate plan state.

    Replay returns the unchanged plan and the entry tally matches the
    appended ops; the drift findings live in evidence files, not in
    plan state.
    """
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
            op="drift_verifier_complete",
            payload={
                "phase_id": "1",
                "passed": False,
                "drift_findings": ["task 1.1: DRIFTED — implemented Y not X"],
                "evidence_path": ".autodev/evidence/1-drift-verifier.json",
            },
            session_id="s1",
        )

    out, entries = replay_ledger(tmp_path)
    assert out is not None
    assert len(entries) == 2
    # Plan state untouched.
    assert out.phases[0].tasks[0].status == "pending"


@pytest.mark.asyncio
async def test_drift_verifier_complete_replays_with_passed_true(
    tmp_path: Path,
) -> None:
    """A ``passed=True`` payload also replays cleanly."""
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
            op="drift_verifier_complete",
            payload={
                "phase_id": "1",
                "passed": True,
                "drift_findings": [],
                "evidence_path": ".autodev/evidence/1-drift-verifier.json",
            },
            session_id="s1",
        )
    out, entries = replay_ledger(tmp_path)
    assert out is not None
    assert len(entries) == 2


@pytest.mark.asyncio
async def test_course_correction_emitted_op_replays_correctly(tmp_path: Path) -> None:
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
            op="course_correction_emitted",
            payload={
                "task_id": "1.1",
                "taxonomy": "reasoning_error",
                "pattern": "repetition_loop",
                "suggestion": "vary the approach; you've made the same edit 3x",
            },
            session_id="s1",
        )

    out, entries = replay_ledger(tmp_path)
    assert out is not None
    assert len(entries) == 2
    assert out.phases[0].tasks[0].status == "pending"


@pytest.mark.asyncio
async def test_v0150_ops_can_appear_before_init_plan(tmp_path: Path) -> None:
    """All four v0.15.0 ops are audit-only and must replay safely even
    when they appear before ``init_plan`` (e.g. lessons emitted during
    plan-tournament BEFORE the executor's plan is persisted)."""
    plan = _mk_plan()
    async with plan_lock(tmp_path):
        await append_entry(
            tmp_path,
            op="course_correction_emitted",
            payload={
                "task_id": "1.1",
                "taxonomy": "reasoning_error",
                "pattern": "repetition_loop",
                "suggestion": "...",
            },
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


# ---------------------------------------------------------------------------
# v0.36.0 round-trip cases for the new ledger ops.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v036_ops_round_trip(tmp_path: Path) -> None:
    """Every v0.36.0 op (architect_attempt_failed, recovery_tier_attempted,
    path_rejection_recorded, network_probe_failed,
    architect_model_changed_for_retry, huge_repo_multiplier_applied,
    retry_budget_scaled, spec_validation_failed) appends + replays as
    an audit-only no-op."""
    plan = _mk_plan()
    async with plan_lock(tmp_path):
        await append_entry(
            tmp_path,
            op="init_plan",
            payload={"plan": plan.model_dump(mode="json")},
            session_id="s1",
        )
    ops = [
        (
            "architect_attempt_failed",
            {
                "attempt": 2,
                "model": "claude-opus-4-7",
                "duration_s": 1.23,
                "rejection_count": 4,
                "primary_class": "new_md_deliverable",
            },
        ),
        (
            "recovery_tier_attempted",
            {
                "tier": 4,
                "outcome": "applied",
                "reason": "recurrent_path_failure",
                "from_state": "undegraded",
                "to_state": "dropped:notes/foo.md",
            },
        ),
        (
            "path_rejection_recorded",
            {"task_id": "1.1", "path": "notes/foo.md", "class": "new_md_deliverable"},
        ),
        (
            "network_probe_failed",
            {
                "adapter": "claude_code",
                "attempt": 3,
                "last_error": "timeout",
                "suggestion": "check VPN",
                "final": True,
            },
        ),
        (
            "architect_model_changed_for_retry",
            {
                "attempt": 2,
                "from_model": "claude-opus-4-7",
                "to_model": "sonnet",
                "rejection_class": "missing_on_disk",
            },
        ),
        (
            "huge_repo_multiplier_applied",
            {
                "role": "explorer",
                "base": 10,
                "multiplier": 3.0,
                "effective": 30,
            },
        ),
        (
            "retry_budget_scaled",
            {"task_id": "1.1", "attempt": 2, "base": 20, "effective": 40},
        ),
        (
            "spec_validation_failed",
            {"path": "fix", "reasons": ["spec_too_short"]},
        ),
    ]
    for op_name, payload in ops:
        async with plan_lock(tmp_path):
            await append_entry(
                tmp_path,
                op=op_name,  # type: ignore[arg-type]
                payload=payload,
                session_id="s1",
            )
    out, entries = replay_ledger(tmp_path)
    assert out is not None
    # 1 init_plan + 8 audit-only ops.
    assert len(entries) == 9
