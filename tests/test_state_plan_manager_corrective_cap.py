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


# ---------------------------------------------------------------------------
# v0.38.0 I3: plan-scope corrective ceiling fixtures.
# ---------------------------------------------------------------------------


def _mk_multi_phase_plan() -> Plan:
    """Three phases, each pre-seeded with 4 existing corrective tasks
    so the plan-wide total starts at 12."""
    phases = []
    for pidx in (1, 2, 3):
        tasks = [
            Task(
                id=f"{pidx}.1",
                phase_id=str(pidx),
                title="t",
                description="d",
                complexity="medium",
                status="complete",
            ),
        ]
        # Pre-existing corrective tasks (will be added separately below
        # via append_corrective_tasks so the plan state mirrors how the
        # orchestrator builds it).
        phases.append(
            Phase(
                id=str(pidx),
                title=f"Phase {pidx}",
                tasks=tasks,
            )
        )
    return Plan(
        plan_id="p-plan-scope",
        spec_hash="0123456789abcdef",
        phases=phases,
        created_at=_iso(),
        updated_at=_iso(),
        complexity="medium",
    )


def _mk_corrective_for(phase_id: str, idx: int) -> Task:
    return Task(
        id=f"{phase_id}.c{idx}",
        phase_id=phase_id,
        title=f"corrective {idx} for {phase_id}",
        description="d",
        complexity="medium",
        assigned_agent="developer",
    )


async def _seed_corrective_counts(pm: PlanManager, per_phase: int) -> None:
    """Seed each of the 3 phases with ``per_phase`` corrective tasks
    (no plan-scope cap so seeding doesn't trip the defence)."""
    for pidx in (1, 2, 3):
        await pm.append_corrective_tasks(
            str(pidx),
            [_mk_corrective_for(str(pidx), i) for i in range(1, per_phase + 1)],
        )


@pytest.mark.asyncio
async def test_plan_scope_cap_truncates_when_total_exceeds_ceiling(
    tmp_path: Path,
) -> None:
    """Plan-wide cap=10 with 12 existing corrective tasks (4 × 3 phases)
    drops a 5-task append entirely and fires the plan-scope defended op."""
    pm = PlanManager(tmp_path, session_id="sess-plan-1")
    await pm.init_plan(_mk_multi_phase_plan())
    await _seed_corrective_counts(pm, per_phase=4)  # total=12 > 10

    new_batch = [_mk_corrective_for("1", i) for i in range(100, 105)]
    await pm.append_corrective_tasks(
        "1", new_batch, max_corrective_tasks_per_plan=10
    )

    plan = await pm.load()
    assert plan is not None
    # No new tasks should land — plan-scope cap is already exceeded.
    assert len(plan.phases[0].corrective_task_ids) == 4

    entries = await pm.read_ledger()
    plan_scope_ops = [
        e
        for e in entries
        if e.op == "corrective_cap_reached"
        and e.payload.get("scope") == "plan"
        and e.payload.get("defended") is True
    ]
    assert len(plan_scope_ops) == 1
    payload = plan_scope_ops[0].payload
    assert payload["cap"] == 10
    assert payload["dropped"] == 5
    assert payload["total_plan_corrective"] == 12
    assert payload["phase_id"] == "1"


@pytest.mark.asyncio
async def test_plan_scope_cap_allows_partial_when_budget_remains(
    tmp_path: Path,
) -> None:
    """Plan-wide cap=20 with 12 existing leaves a budget of 8; a 5-task
    append lands all 5 (16 ≤ 20). No defensive op fires."""
    pm = PlanManager(tmp_path, session_id="sess-plan-2")
    await pm.init_plan(_mk_multi_phase_plan())
    await _seed_corrective_counts(pm, per_phase=4)  # total=12

    new_batch = [_mk_corrective_for("1", i) for i in range(100, 105)]
    await pm.append_corrective_tasks(
        "1", new_batch, max_corrective_tasks_per_plan=20
    )

    plan = await pm.load()
    assert plan is not None
    # All 5 new tasks land on phase 1 (4 prior + 5 new = 9).
    assert len(plan.phases[0].corrective_task_ids) == 9

    entries = await pm.read_ledger()
    plan_scope_ops = [
        e
        for e in entries
        if e.op == "corrective_cap_reached"
        and e.payload.get("scope") == "plan"
    ]
    assert plan_scope_ops == []


@pytest.mark.asyncio
async def test_plan_scope_cap_truncates_to_fit_remaining_budget(
    tmp_path: Path,
) -> None:
    """Plan-wide cap=15 with 12 existing leaves a budget of 3; a 5-task
    append lands the first 3, drops the trailing 2."""
    pm = PlanManager(tmp_path, session_id="sess-plan-3")
    await pm.init_plan(_mk_multi_phase_plan())
    await _seed_corrective_counts(pm, per_phase=4)  # total=12

    new_batch = [_mk_corrective_for("1", i) for i in range(100, 105)]
    await pm.append_corrective_tasks(
        "1", new_batch, max_corrective_tasks_per_plan=15
    )

    plan = await pm.load()
    assert plan is not None
    assert len(plan.phases[0].corrective_task_ids) == 4 + 3  # 7

    entries = await pm.read_ledger()
    plan_scope_ops = [
        e
        for e in entries
        if e.op == "corrective_cap_reached"
        and e.payload.get("scope") == "plan"
        and e.payload.get("defended") is True
    ]
    assert len(plan_scope_ops) == 1
    assert plan_scope_ops[0].payload["dropped"] == 2


@pytest.mark.asyncio
async def test_plan_scope_cap_wins_when_smaller_than_per_phase_cap(
    tmp_path: Path,
) -> None:
    """Both caps active: plan-scope ceiling fires first (smaller), then
    the per-phase ceiling runs against the trimmed batch. The plan-scope
    op carries scope="plan"; if the per-phase ceiling also fires it
    carries scope="phase"."""
    pm = PlanManager(tmp_path, session_id="sess-plan-4")
    await pm.init_plan(_mk_multi_phase_plan())
    await _seed_corrective_counts(pm, per_phase=4)  # total=12

    # Plan cap=15 (budget=3); per-phase cap=8 (budget for phase 1 = 4).
    # The plan-scope cap is binding — 5 → 3 truncation, no per-phase
    # firing because 3 ≤ 4 already.
    new_batch = [_mk_corrective_for("1", i) for i in range(100, 105)]
    await pm.append_corrective_tasks(
        "1",
        new_batch,
        max_corrective_tasks_per_phase=8,
        max_corrective_tasks_per_plan=15,
    )

    plan = await pm.load()
    assert plan is not None
    assert len(plan.phases[0].corrective_task_ids) == 7  # 4 + 3

    entries = await pm.read_ledger()
    plan_scope_ops = [
        e
        for e in entries
        if e.op == "corrective_cap_reached"
        and e.payload.get("scope") == "plan"
        and e.payload.get("defended") is True
    ]
    phase_scope_ops = [
        e
        for e in entries
        if e.op == "corrective_cap_reached"
        and e.payload.get("scope") == "phase"
        and e.payload.get("defended") is True
    ]
    assert len(plan_scope_ops) == 1
    assert plan_scope_ops[0].payload["dropped"] == 2
    assert phase_scope_ops == []


@pytest.mark.asyncio
async def test_phase_scope_cap_payload_carries_scope_field(
    tmp_path: Path,
) -> None:
    """v0.38.0 I3: the per-phase defensive cap op now carries
    ``scope="phase"`` in its payload (additive — old tests still pass
    by not asserting on it)."""
    pm = PlanManager(tmp_path, session_id="sess-plan-5")
    await pm.init_plan(_mk_plan())

    new = [_mk_corrective(i) for i in range(1, 8)]  # 7 tasks
    await pm.append_corrective_tasks(
        "1", new, max_corrective_tasks_per_phase=5
    )

    entries = await pm.read_ledger()
    cap_ops = [e for e in entries if e.op == "corrective_cap_reached"]
    assert len(cap_ops) == 1
    payload = cap_ops[0].payload
    assert payload.get("scope") == "phase"
    assert payload["defended"] is True
    assert payload["dropped"] == 2
