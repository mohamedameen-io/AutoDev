"""Tests for ``state.rewind.apply_rewind`` (v0.29.0 Bug 9).

Apply must be idempotent — re-running on a workspace that has already
been rewound writes zero new ledger entries. Evidence and tournament
artifacts referencing post-target phases are MOVED (not deleted) into a
timestamped sub-directory under ``.autodev/rewound/`` so a forensic
post-mortem can still inspect what was force-accepted by mistake.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
from pathlib import Path

from state.plan_manager import PlanManager
from state.rewind import apply_rewind
from state.schemas import Phase, Plan, Task


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan() -> Plan:
    """Four-phase plan: 0, 0.5, 1, 2 — one task per phase. Task in
    phase 1 is ``coded`` (mid-work); task in phase 2 is ``blocked``;
    tasks in 0 and 0.5 are ``complete``. Phases 0/0.5 are accepted,
    phases 1 and 2 have ``review_status`` ``"corrective_required"``
    and ``"in_progress"`` respectively (the post-rewind state should
    flip both back to ``None``)."""
    return Plan(
        plan_id="p-rewind-apply",
        spec_hash="0123456789abcdef",
        phases=[
            Phase(
                id="0",
                title="Phase 0",
                review_status="accepted",
                tasks=[
                    Task(
                        id="0.1",
                        phase_id="0",
                        title="t0.1",
                        description="d",
                        status="complete",
                    ),
                ],
            ),
            Phase(
                id="0.5",
                title="Phase 0.5",
                review_status="accepted",
                tasks=[
                    Task(
                        id="0.5.1",
                        phase_id="0.5",
                        title="t0.5.1",
                        description="d",
                        status="complete",
                    ),
                ],
            ),
            Phase(
                id="1",
                title="Phase 1",
                review_status="corrective_required",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="t1.1",
                        description="d",
                        status="coded",
                        retry_count=2,
                        escalated=True,
                    ),
                ],
            ),
            Phase(
                id="2",
                title="Phase 2",
                review_status="in_progress",
                tasks=[
                    Task(
                        id="2.1",
                        phase_id="2",
                        title="t2.1",
                        description="d",
                        status="blocked",
                        blocked_reason="auth_failed: 403",
                        retry_count=1,
                    ),
                ],
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
    )


def _seed(cwd: Path, plan: Plan) -> None:
    pm = PlanManager(cwd, session_id="sess-apply-init")

    async def _init() -> None:
        await pm.init_plan(plan)

    asyncio.run(_init())


def _read_ledger_ops(cwd: Path) -> list[str]:
    lp = cwd / ".autodev" / "plan-ledger.jsonl"
    if not lp.exists():
        return []
    out: list[str] = []
    for raw in lp.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        out.append(json.loads(line)["op"])
    return out


def _read_ledger_entries_raw(cwd: Path) -> list[dict]:
    lp = cwd / ".autodev" / "plan-ledger.jsonl"
    if not lp.exists():
        return []
    out: list[dict] = []
    for raw in lp.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


# ---------------------------------------------------------------------------
# Test 1 — apply resets tasks in phases AFTER target. Tasks in target +
# earlier phases are untouched.
# ---------------------------------------------------------------------------


def test_apply_resets_tasks_in_phases_after_target(tmp_path: Path) -> None:
    cwd = tmp_path
    _seed(cwd, _mk_plan())

    pm = PlanManager(cwd, session_id="sess-apply-rewind")

    async def _go() -> None:
        result = await apply_rewind(cwd, "0.5", pm)
        assert result.target_phase_id == "0.5"
        assert sorted(result.reset_task_ids) == ["1.1", "2.1"]

    asyncio.run(_go())

    # Reload + assert.
    pm_read = PlanManager(cwd, session_id="sess-apply-readback")

    async def _load() -> Plan:
        plan = await pm_read.load()
        assert plan is not None
        return plan

    plan = asyncio.run(_load())
    by_id = {t.id: t for phase in plan.phases for t in phase.tasks}
    # Untouched (target + earlier).
    assert by_id["0.1"].status == "complete"
    assert by_id["0.5.1"].status == "complete"
    # Reset (after target).
    assert by_id["1.1"].status == "pending"
    assert by_id["1.1"].retry_count == 0
    assert by_id["1.1"].escalated is False
    assert by_id["2.1"].status == "pending"
    assert by_id["2.1"].blocked_reason is None


# ---------------------------------------------------------------------------
# Test 2 — phase review_status cleared for after-target phases ONLY.
# ---------------------------------------------------------------------------


def test_apply_clears_review_status_for_phases_after_target(
    tmp_path: Path,
) -> None:
    cwd = tmp_path
    _seed(cwd, _mk_plan())

    pm = PlanManager(cwd, session_id="sess-apply-rewind")

    async def _go() -> None:
        result = await apply_rewind(cwd, "0.5", pm)
        assert sorted(result.reset_phase_ids) == ["1", "2"]

    asyncio.run(_go())

    pm_read = PlanManager(cwd, session_id="sess-apply-readback")

    async def _load() -> Plan:
        plan = await pm_read.load()
        assert plan is not None
        return plan

    plan = asyncio.run(_load())
    by_phase = {p.id: p for p in plan.phases}
    # Untouched.
    assert by_phase["0"].review_status == "accepted"
    assert by_phase["0.5"].review_status == "accepted"
    # Cleared.
    assert by_phase["1"].review_status is None
    assert by_phase["2"].review_status is None


# ---------------------------------------------------------------------------
# Test 3 — evidence/tournament artifacts archived under .autodev/rewound/.
# ---------------------------------------------------------------------------


def test_apply_archives_evidence_to_autodev_rewound_dir(tmp_path: Path) -> None:
    cwd = tmp_path
    _seed(cwd, _mk_plan())

    # Seed evidence + tournament artifacts referencing each phase.
    ev = cwd / ".autodev" / "evidence"
    ev.mkdir(parents=True, exist_ok=True)
    (ev / "0.1-developer.json").write_text(
        json.dumps({"task_id": "0.1", "kind": "developer"}), encoding="utf-8"
    )
    (ev / "1.1-developer.json").write_text(
        json.dumps({"task_id": "1.1", "kind": "developer"}), encoding="utf-8"
    )
    (ev / "2.1-developer.json").write_text(
        json.dumps({"task_id": "2.1", "kind": "developer"}), encoding="utf-8"
    )
    td = cwd / ".autodev" / "tournaments"
    td.mkdir(parents=True, exist_ok=True)
    (td / "phase-review-1").mkdir()
    (td / "phase-review-1" / "history.json").write_text("[]", encoding="utf-8")
    (td / "phase-review-2").mkdir()
    (td / "phase-review-2" / "history.json").write_text("[]", encoding="utf-8")

    pm = PlanManager(cwd, session_id="sess-apply-rewind")

    async def _go() -> None:
        result = await apply_rewind(cwd, "0.5", pm)
        assert result.archive_dir is not None
        assert result.archive_dir.exists()
        # Phase 0.1 evidence stays in place; 1.1 + 2.1 + the two
        # phase-review tournament dirs migrate.
        archived_names = {p.name for p in result.archived_paths}
        assert "1.1-developer.json" in archived_names
        assert "2.1-developer.json" in archived_names
        assert "phase-review-1" in archived_names
        assert "phase-review-2" in archived_names
        assert "0.1-developer.json" not in archived_names

    asyncio.run(_go())

    # Assert physical migration: source files gone, destination present.
    assert not (ev / "1.1-developer.json").exists()
    assert not (ev / "2.1-developer.json").exists()
    assert not (td / "phase-review-1").exists()
    assert not (td / "phase-review-2").exists()
    # 0.1 must remain in place (it belongs to a pre-target phase).
    assert (ev / "0.1-developer.json").exists()
    # Archive dir naming: <stamp>-<phase_id> under .autodev/rewound/.
    rewound_root = cwd / ".autodev" / "rewound"
    assert rewound_root.exists()
    children = list(rewound_root.iterdir())
    assert len(children) == 1
    assert children[0].name.endswith("-0.5")


# ---------------------------------------------------------------------------
# Test 4 — idempotent: a second apply on a fresh state writes 0 new ops.
# ---------------------------------------------------------------------------


def test_apply_idempotent(tmp_path: Path) -> None:
    cwd = tmp_path
    _seed(cwd, _mk_plan())

    pm = PlanManager(cwd, session_id="sess-apply-rewind")

    async def _first() -> None:
        await apply_rewind(cwd, "0.5", pm)

    asyncio.run(_first())
    ops_after_first = _read_ledger_ops(cwd)

    async def _second() -> None:
        result = await apply_rewind(cwd, "0.5", pm)
        # No new resets needed.
        assert result.reset_task_ids == []
        assert result.reset_phase_ids == []

    asyncio.run(_second())
    ops_after_second = _read_ledger_ops(cwd)

    # Zero new ledger entries — strict idempotency. The ``rewind``
    # breadcrumb is suppressed when the diff is empty.
    assert ops_after_second == ops_after_first


# ---------------------------------------------------------------------------
# Test 5 — the audit ``rewind`` ledger entry lands with the expected
# payload shape.
# ---------------------------------------------------------------------------


def test_apply_appends_rewind_ledger_entry(tmp_path: Path) -> None:
    cwd = tmp_path
    _seed(cwd, _mk_plan())

    pm = PlanManager(cwd, session_id="sess-apply-rewind")

    async def _go() -> None:
        await apply_rewind(cwd, "0.5", pm)

    asyncio.run(_go())

    entries = _read_ledger_entries_raw(cwd)
    rewind_entries = [e for e in entries if e["op"] == "rewind"]
    assert len(rewind_entries) == 1
    payload = rewind_entries[0]["payload"]
    assert payload["target_phase_id"] == "0.5"
    assert sorted(payload["reset_task_ids"]) == ["1.1", "2.1"]
    assert sorted(payload["reset_phase_ids"]) == ["1", "2"]
    # Archive dir is None when no artifacts existed (this test seeds
    # no evidence/tournament files).
    assert payload["archive_dir"] is None
    assert payload["archived_paths"] == []
