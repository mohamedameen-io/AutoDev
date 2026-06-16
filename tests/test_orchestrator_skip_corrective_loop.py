"""v0.38.0 I3 (HK5): per-phase ``skip_corrective_count`` counter on
``Phase.metadata`` detects stuck ``skip_corrective_round`` loops.

The orchestrator increments the counter on every cap-hit that takes
the ``skip_corrective_round`` branch (both architect-refine and
phase-review sites). A successful corrective injection resets it to 0.
When the counter reaches 3 the orchestrator emits a structured warning
+ a ``skip_corrective_loop_detected`` ledger op. v0.38.0 is diagnostic-
only — no auto-soft-block — so these tests pin the observability
contract without prescribing recovery.

Tests use the module-level helpers directly (``_bump_skip_corrective_counter``
+ ``_reset_skip_corrective_counter``) because the integration test in
``tests/integration/test_v038_skip_corrective_loop.py`` (future work)
will pin the end-to-end orchestrator wiring; this file targets the
counter mechanics.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from pathlib import Path

import pytest

from orchestrator.execute_phase import (
    _bump_skip_corrective_counter,
    _reset_skip_corrective_counter,
)
from state.plan_manager import PlanManager
from state.schemas import Phase, Plan, Task


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan() -> Plan:
    return Plan(
        plan_id="p-skip-loop",
        spec_hash="0123456789abcdef",
        phases=[
            Phase(
                id="1",
                title="Investigate",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="t",
                        description="d",
                        complexity="medium",
                        status="complete",
                    ),
                ],
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
        complexity="medium",
    )


@dataclass
class _FakeCfg:
    corrective_cap_action: str = "skip_corrective_round"


@dataclass
class _FakeOrch:
    cwd: Path
    cfg: _FakeCfg
    plan_manager: PlanManager


@pytest.mark.asyncio
async def test_single_skip_increments_counter_to_one(
    tmp_path: Path,
) -> None:
    """One ``skip_corrective_round`` bump leaves the counter at 1 and
    fires NO warning / ledger op (below threshold)."""
    pm = PlanManager(tmp_path, session_id="sess-skip-1")
    await pm.init_plan(_mk_plan())
    orch = _FakeOrch(cwd=tmp_path, cfg=_FakeCfg(), plan_manager=pm)

    await _bump_skip_corrective_counter(
        orch,  # type: ignore[arg-type]
        phase_id="1",
        cap_action="skip_corrective_round",
    )

    plan = await pm.load()
    assert plan is not None
    assert plan.phases[0].metadata.get("skip_corrective_count") == 1

    entries = await pm.read_ledger()
    loop_ops = [
        e for e in entries if e.op == "skip_corrective_loop_detected"
    ]
    assert loop_ops == []


@pytest.mark.asyncio
async def test_three_consecutive_skips_fire_loop_detected_op(
    tmp_path: Path,
) -> None:
    """Three consecutive bumps reach the threshold; the
    ``skip_corrective_loop_detected`` ledger op fires on the third."""
    pm = PlanManager(tmp_path, session_id="sess-skip-2")
    await pm.init_plan(_mk_plan())
    orch = _FakeOrch(cwd=tmp_path, cfg=_FakeCfg(), plan_manager=pm)

    for _ in range(3):
        await _bump_skip_corrective_counter(
            orch,  # type: ignore[arg-type]
            phase_id="1",
            cap_action="skip_corrective_round",
        )

    plan = await pm.load()
    assert plan is not None
    assert plan.phases[0].metadata.get("skip_corrective_count") == 3

    entries = await pm.read_ledger()
    loop_ops = [
        e for e in entries if e.op == "skip_corrective_loop_detected"
    ]
    assert len(loop_ops) == 1
    payload = loop_ops[0].payload
    assert payload["phase_id"] == "1"
    assert payload["count"] == 3
    assert payload["action"] == "skip_corrective_round"


@pytest.mark.asyncio
async def test_successful_round_resets_counter_to_zero(
    tmp_path: Path,
) -> None:
    """Two skips → counter=2; a successful corrective round resets it
    to 0; a third skip after the reset leaves the counter at 1 (not 3),
    so no warning fires."""
    pm = PlanManager(tmp_path, session_id="sess-skip-3")
    await pm.init_plan(_mk_plan())
    orch = _FakeOrch(cwd=tmp_path, cfg=_FakeCfg(), plan_manager=pm)

    await _bump_skip_corrective_counter(
        orch,  # type: ignore[arg-type]
        phase_id="1",
        cap_action="skip_corrective_round",
    )
    await _bump_skip_corrective_counter(
        orch,  # type: ignore[arg-type]
        phase_id="1",
        cap_action="skip_corrective_round",
    )

    plan = await pm.load()
    assert plan is not None
    assert plan.phases[0].metadata.get("skip_corrective_count") == 2

    # Reset on successful corrective round.
    await _reset_skip_corrective_counter(orch, phase_id="1")  # type: ignore[arg-type]

    plan = await pm.load()
    assert plan is not None
    assert plan.phases[0].metadata.get("skip_corrective_count") == 0

    # Another skip after the reset.
    await _bump_skip_corrective_counter(
        orch,  # type: ignore[arg-type]
        phase_id="1",
        cap_action="skip_corrective_round",
    )

    plan = await pm.load()
    assert plan is not None
    assert plan.phases[0].metadata.get("skip_corrective_count") == 1

    entries = await pm.read_ledger()
    loop_ops = [
        e for e in entries if e.op == "skip_corrective_loop_detected"
    ]
    assert loop_ops == []


@pytest.mark.asyncio
async def test_counter_persists_across_load(tmp_path: Path) -> None:
    """The counter is replayed from the ledger ``update_phase_meta`` ops
    so a fresh PlanManager rebuilds the value on load."""
    pm = PlanManager(tmp_path, session_id="sess-skip-4")
    await pm.init_plan(_mk_plan())
    orch = _FakeOrch(cwd=tmp_path, cfg=_FakeCfg(), plan_manager=pm)

    await _bump_skip_corrective_counter(
        orch,  # type: ignore[arg-type]
        phase_id="1",
        cap_action="skip_corrective_round",
    )
    await _bump_skip_corrective_counter(
        orch,  # type: ignore[arg-type]
        phase_id="1",
        cap_action="skip_corrective_round",
    )

    # Reload from disk to exercise replay of update_phase_meta with the
    # metadata field merged in.
    pm2 = PlanManager(tmp_path, session_id="sess-skip-4-replay")
    plan = await pm2.load()
    assert plan is not None
    assert plan.phases[0].metadata.get("skip_corrective_count") == 2


@pytest.mark.asyncio
async def test_reset_on_zero_counter_is_noop(tmp_path: Path) -> None:
    """Resetting the counter when it is already 0 emits NO redundant
    ``update_phase_meta`` op — important so the happy path doesn't
    spam the ledger with no-op resets."""
    pm = PlanManager(tmp_path, session_id="sess-skip-5")
    await pm.init_plan(_mk_plan())
    orch = _FakeOrch(cwd=tmp_path, cfg=_FakeCfg(), plan_manager=pm)

    entries_before = await pm.read_ledger()
    meta_count_before = sum(
        1
        for e in entries_before
        if e.op == "update_phase_meta"
        and "metadata" in (e.payload or {})
    )

    await _reset_skip_corrective_counter(orch, phase_id="1")  # type: ignore[arg-type]

    entries_after = await pm.read_ledger()
    meta_count_after = sum(
        1
        for e in entries_after
        if e.op == "update_phase_meta"
        and "metadata" in (e.payload or {})
    )
    assert meta_count_after == meta_count_before


@pytest.mark.asyncio
async def test_counter_continues_incrementing_past_threshold(
    tmp_path: Path,
) -> None:
    """Bumps past the threshold continue to fire the loop-detected op
    on each subsequent skip — operators see escalating frequency."""
    pm = PlanManager(tmp_path, session_id="sess-skip-6")
    await pm.init_plan(_mk_plan())
    orch = _FakeOrch(cwd=tmp_path, cfg=_FakeCfg(), plan_manager=pm)

    for _ in range(5):
        await _bump_skip_corrective_counter(
            orch,  # type: ignore[arg-type]
            phase_id="1",
            cap_action="skip_corrective_round",
        )

    plan = await pm.load()
    assert plan is not None
    assert plan.phases[0].metadata.get("skip_corrective_count") == 5

    entries = await pm.read_ledger()
    loop_ops = [
        e for e in entries if e.op == "skip_corrective_loop_detected"
    ]
    # Fires on count 3, 4, 5 → 3 total ops.
    assert len(loop_ops) == 3
    assert [op.payload["count"] for op in loop_ops] == [3, 4, 5]
