"""Tests for v0.29.0 Bug 6: typed ``Task.block_reason_class`` field.

The class stamps the *category* of failure that drove the block:

  * ``"verdict"``        — agent reached a legitimate negative verdict
                          (reviewer rejected, tests failed past retry,
                          architect concluded the work is wrong).
  * ``"infrastructure"`` — outside-the-loop transient failure (auth
                          refresh, gateway 4xx, network blip, timeout).
                          Safely requeueable once the operator fixes
                          the underlying environment.
  * ``"cap"``            — the agent ran out of turns / tokens / budget
                          legitimately (ate the entire allowed effort
                          on the work, didn't get there). Distinct from
                          ``"infrastructure"`` because requeueing won't
                          help — the operator needs to widen budget or
                          decompose the task.

Default ``None`` for backward compat with on-disk plans written before
v0.29.0. ``update_task_status`` accepts a ``block_reason_class`` key
in ``meta`` and merges it into the Task plus the ledger payload.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import pytest

from state.plan_manager import PlanManager
from state.schemas import Phase, Plan, Task


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan() -> Plan:
    return Plan(
        plan_id="p-block-class",
        spec_hash="cafebabe",
        phases=[
            Phase(
                id="1",
                title="Setup",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="task a",
                        description="do a",
                    ),
                ],
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
    )


@pytest.mark.asyncio
async def test_block_with_explicit_class_stored(tmp_path: Path) -> None:
    """update_task_status with meta containing block_reason_class merges
    the typed enum into both the in-memory Task and the persisted plan
    snapshot. Round-trips through plan reload.
    """
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())

    # Transition through the FSM to a state where block is allowed.
    await pm.update_task_status("1.1", "in_progress")
    task = await pm.update_task_status(
        "1.1",
        "blocked",
        meta={
            "blocked_reason": "auth_failed: 403 Forbidden from upstream",
            "block_reason_class": "infrastructure",
        },
    )
    assert task.block_reason_class == "infrastructure"
    assert task.blocked_reason == "auth_failed: 403 Forbidden from upstream"

    # Re-load via a fresh PlanManager so we exercise the snapshot path.
    pm2 = PlanManager(tmp_path, session_id="s2")
    plan = await pm2.load()
    assert plan is not None
    reloaded = plan.phases[0].tasks[0]
    assert reloaded.status == "blocked"
    assert reloaded.block_reason_class == "infrastructure"


@pytest.mark.asyncio
async def test_load_legacy_plan_backfills_class_from_reason_string(
    tmp_path: Path,
) -> None:
    """A v0.28.0-shape plan.json (status=blocked, no block_reason_class)
    must load cleanly. The migration shim inspects ``blocked_reason`` and
    backfills ``block_reason_class`` using the same keyword heuristic
    used by ``autodev requeue --infrastructure`` (Bug 8).
    """
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())

    # Hand-write a legacy snapshot with a blocked task missing the new
    # field. Use the in-memory Task -> dict path so we have a real
    # plan-shaped payload to round-trip.
    plan = await pm.load()
    assert plan is not None
    payload = plan.model_dump(mode="json")
    payload["phases"][0]["tasks"][0]["status"] = "blocked"
    payload["phases"][0]["tasks"][0]["blocked_reason"] = (
        "auth_failed: 401 unauthorized"
    )
    # Remove the field entirely to simulate a pre-v0.29.0 on-disk dict.
    payload["phases"][0]["tasks"][0].pop("block_reason_class", None)

    # Overwrite the latest snapshot ledger entry with the legacy shape,
    # and re-rewrite every subsequent entry's prev_hash + self_hash to
    # keep the chain valid (the ledger replay refuses to load a chain
    # with a broken self_hash). We mutate only the snapshot's payload
    # in this test, so only entries from the snapshot onward need
    # re-hashing.
    from state.ledger import compute_hash
    from state.paths import ledger_path

    ledger = ledger_path(tmp_path)
    lines = ledger.read_text(encoding="utf-8").splitlines()
    snapshot_idx = next(
        (
            i
            for i in range(len(lines) - 1, -1, -1)
            if json.loads(lines[i]).get("op") == "snapshot"
        ),
        None,
    )
    assert snapshot_idx is not None
    prev_hash = ""
    for i, raw in enumerate(lines):
        entry = json.loads(raw)
        if i == snapshot_idx:
            entry["payload"]["plan"] = payload
        if i >= snapshot_idx:
            entry["prev_hash"] = prev_hash
            body = {k: v for k, v in entry.items() if k != "self_hash"}
            entry["self_hash"] = compute_hash(body)
        prev_hash = entry["self_hash"]
        lines[i] = json.dumps(entry)
    ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Re-load via a fresh PlanManager: the migration shim should
    # backfill block_reason_class="infrastructure" from the auth/401
    # substring in blocked_reason.
    pm2 = PlanManager(tmp_path, session_id="s2")
    reloaded = await pm2.load()
    assert reloaded is not None
    task = reloaded.phases[0].tasks[0]
    assert task.status == "blocked"
    assert task.block_reason_class == "infrastructure"
