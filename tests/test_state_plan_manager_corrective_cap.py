"""v0.37.0 H2: defensive corrective-task cap inside
:meth:`PlanManager.append_corrective_tasks`.

The orchestrator computes the per-phase remaining budget upstream and
threads it into the parser via ``max_tasks``. This is the defence-in-depth
layer: a future caller (CLI helper, ad-hoc script, replay) that bypasses
the upstream computation must still not overflow the cap. When the
defensive truncation fires, a ``corrective_cap_reached`` ledger op
carries ``defended=True`` so dashboards can spot upstream regressions
separately from legitimate orchestrator-level cap hits.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from state.plan_manager import PlanManager
from state.schemas import Phase, Plan, Task


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan() -> Plan:
    return Plan(
        plan_id="p-defense",
        spec_hash="cafebabecafebabe",
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


def _mk_corrective(idx: int) -> Task:
    return Task(
        id=f"1.c{idx}",
        phase_id="1",
        title=f"corrective {idx}",
        description="d",
        complexity="medium",
        assigned_agent="developer",
    )


@pytest.mark.asyncio
async def test_append_corrective_tasks_truncates_at_cap(tmp_path: Path) -> None:
    """A single call passing 7 tasks with ``cap=5`` lands 5 tasks and
    fires the defended ledger op for the dropped 2."""
    pm = PlanManager(tmp_path, session_id="sess-defense-1")
    await pm.init_plan(_mk_plan())

    new = [_mk_corrective(i) for i in range(1, 8)]  # 7 tasks
    await pm.append_corrective_tasks(
        "1", new, max_corrective_tasks_per_phase=5
    )

    plan = await pm.load()
    assert plan is not None
    phase = plan.phases[0]
    assert len(phase.corrective_task_ids) == 5

    entries = await pm.read_ledger()
    cap_ops = [e for e in entries if e.op == "corrective_cap_reached"]
    assert len(cap_ops) == 1
    payload = cap_ops[0].payload
    assert payload["defended"] is True
    assert payload["dropped"] == 2
    assert payload["cap"] == 5


@pytest.mark.asyncio
async def test_append_corrective_tasks_truncates_against_existing(
    tmp_path: Path,
) -> None:
    """The cap is cumulative against the phase's existing
    ``corrective_task_ids``: 3 already there + a 5-task batch with
    cap=5 lands 2 new tasks, drops 3, fires the defended op once."""
    pm = PlanManager(tmp_path, session_id="sess-defense-2")
    await pm.init_plan(_mk_plan())

    # Pre-seed 3 corrective tasks (under the cap — no defensive op yet).
    await pm.append_corrective_tasks(
        "1",
        [_mk_corrective(i) for i in range(1, 4)],
        max_corrective_tasks_per_phase=5,
    )
    plan = await pm.load()
    assert plan is not None
    assert len(plan.phases[0].corrective_task_ids) == 3

    # Second call: 5 new tasks; only 2 should fit.
    second_batch = [_mk_corrective(i) for i in range(4, 9)]  # ids c4..c8
    await pm.append_corrective_tasks(
        "1", second_batch, max_corrective_tasks_per_phase=5
    )

    plan = await pm.load()
    assert plan is not None
    assert len(plan.phases[0].corrective_task_ids) == 5

    entries = await pm.read_ledger()
    cap_ops = [
        e
        for e in entries
        if e.op == "corrective_cap_reached"
        and e.payload.get("defended") is True
    ]
    assert len(cap_ops) == 1
    assert cap_ops[0].payload["dropped"] == 3


@pytest.mark.asyncio
async def test_append_corrective_tasks_no_cap_arg_preserves_legacy_behaviour(
    tmp_path: Path,
) -> None:
    """When the cap arg is not supplied (legacy call shape), the function
    appends everything and fires NO defensive op — backward compatible
    with v0.36.x and earlier."""
    pm = PlanManager(tmp_path, session_id="sess-defense-3")
    await pm.init_plan(_mk_plan())

    new = [_mk_corrective(i) for i in range(1, 11)]  # 10 tasks
    await pm.append_corrective_tasks("1", new)

    plan = await pm.load()
    assert plan is not None
    assert len(plan.phases[0].corrective_task_ids) == 10

    entries = await pm.read_ledger()
    cap_ops = [e for e in entries if e.op == "corrective_cap_reached"]
    assert cap_ops == []


@pytest.mark.asyncio
async def test_append_corrective_tasks_at_cap_drops_all_new(
    tmp_path: Path,
) -> None:
    """When the phase is already at the cap, a fresh batch is fully
    dropped and the defensive op records the full input length."""
    pm = PlanManager(tmp_path, session_id="sess-defense-4")
    await pm.init_plan(_mk_plan())

    # Fill to cap=3 first.
    await pm.append_corrective_tasks(
        "1",
        [_mk_corrective(i) for i in range(1, 4)],
        max_corrective_tasks_per_phase=3,
    )
    # A second batch finds budget=0 — all 4 new tasks dropped.
    await pm.append_corrective_tasks(
        "1",
        [_mk_corrective(i) for i in range(4, 8)],
        max_corrective_tasks_per_phase=3,
    )

    plan = await pm.load()
    assert plan is not None
    assert len(plan.phases[0].corrective_task_ids) == 3

    entries = await pm.read_ledger()
    cap_ops = [
        e
        for e in entries
        if e.op == "corrective_cap_reached"
        and e.payload.get("defended") is True
    ]
    assert len(cap_ops) == 1
    assert cap_ops[0].payload["dropped"] == 4
