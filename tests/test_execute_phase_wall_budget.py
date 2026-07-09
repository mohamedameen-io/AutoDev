"""Task 2 (wall-budget fix, DAG-wide sibling of the impl/plan wall budgets):
cumulative WALL-CLOCK ceiling spanning a WHOLE ``run_execute_phase()``
invocation — across however many tasks, retries, and tournaments run serially
before the ``autodev execute`` / ``autodev resume`` command returns.

Distinct from ``max_duration_s_per_task`` (bounds ONE task's ``delegate()``
round-trips) and ``impl_phase_wall_budget_s`` (bounds ONE impl tournament's own
pass loop): this bounds the SUM across the whole execute-phase DAG, which is
what actually would have caught the SWE-bench-Lite pilot 1800s SIGKILL (4 impl
tournaments across 4 tasks, serial under ``max_parallel_subprocesses=1``).

Design note (Task 1 lesson): the integration tests DRIVE THE REAL code paths —
the real ``GuardrailEnforcer.check_execute_phase_wall_budget`` raise, the real
``_execute_phase_dag`` spawn-gating, the real ``_execute_one`` retry-loop check,
the real top-level ``except`` + ledger op, and the real
``PlanManager.reap_orphans`` salvage. Only the *clock* is faked (deterministic
time) and only ``_execute_one``'s dev work is stubbed (to avoid live claude) —
NOT the wall-budget exception itself, which is always produced by the real
enforcer from a real budget + fake clock.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

import pytest

from agents import build_registry
from config.defaults import default_config
from config.schema import GuardrailsConfig
from errors import (
    AutodevError,
    ExecutePhaseWallBudgetExceededError,
    TournamentError,
)
from guardrails.enforcer import GuardrailEnforcer
from orchestrator import Orchestrator
from orchestrator import execute_phase as ep
from state.plan_manager import PlanManager
from state.schemas import AcceptanceCriterion, Phase, Plan, Task

from stub_adapter import StubAdapter


# ── deterministic fake clock ────────────────────────────────────────────────


class FakeClock:
    """Controllable monotonic-shaped clock. Reads do NOT auto-advance — the
    test calls :meth:`advance` explicitly at the exact point it wants time to
    pass, which keeps the wall-budget arithmetic fully deterministic."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _cfg(**kwargs: Any) -> GuardrailsConfig:
    defaults = {
        "max_invocations_per_task": 10,
        "max_tool_calls_per_task": 10,
        "max_duration_s_per_task": 60,
        "max_diff_bytes": 1024,
    }
    defaults.update(kwargs)
    return GuardrailsConfig(**defaults)


# ── plan fixtures ────────────────────────────────────────────────────────────


def _mk_single_task_plan() -> Plan:
    return Plan(
        plan_id="p-exec-wall-1",
        spec_hash="0123456789abcdef",
        phases=[
            Phase(
                id="1",
                title="Work",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="t1",
                        description="d1",
                        complexity="medium",
                    ),
                ],
                acceptance=[
                    AcceptanceCriterion(id="ph-ac-1", description="ok")
                ],
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
        complexity="medium",
    )


def _mk_two_task_plan() -> Plan:
    """Two tasks in one phase; ``1.2`` depends on ``1.1`` so the DAG runs them
    strictly serially (1.1, then 1.2) — mirroring the incident's serial
    execution under a parallelism cap of 1."""
    return Plan(
        plan_id="p-exec-wall-2",
        spec_hash="0123456789abcdef",
        phases=[
            Phase(
                id="1",
                title="Work",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="t1",
                        description="d1",
                        complexity="medium",
                    ),
                    Task(
                        id="1.2",
                        phase_id="1",
                        title="t2",
                        description="d2",
                        complexity="medium",
                        depends_on=["1.1"],
                    ),
                ],
                acceptance=[
                    AcceptanceCriterion(id="ph-ac-1", description="ok")
                ],
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
        complexity="medium",
    )


def _mk_two_phase_plan() -> Plan:
    """One task per phase across two phases (sequential phase execution)."""
    return Plan(
        plan_id="p-exec-wall-3",
        spec_hash="0123456789abcdef",
        phases=[
            Phase(
                id="1",
                title="P1",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="t1",
                        description="d1",
                        complexity="medium",
                    ),
                ],
                acceptance=[AcceptanceCriterion(id="a1", description="ok")],
            ),
            Phase(
                id="2",
                title="P2",
                tasks=[
                    Task(
                        id="2.1",
                        phase_id="2",
                        title="t2",
                        description="d2",
                        complexity="medium",
                    ),
                ],
                acceptance=[AcceptanceCriterion(id="a2", description="ok")],
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
        complexity="medium",
    )


def _make_orch(cwd: Path, *, execute_phase_wall_budget_s: float | None) -> Orchestrator:
    """Orchestrator with all tournaments + phase-review OFF so the DAG path is
    driven purely by a (monkeypatched) ``_execute_one`` — no live adapter."""
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    cfg.tournaments.phase_review.enabled = False
    cfg.tournaments.auto_disable_for_models = []
    # Force strictly-serial task dispatch (the incident's shape).
    cfg.tournaments.execute_max_parallel_tasks = 1
    cfg.guardrails.execute_phase_wall_budget_s = execute_phase_wall_budget_s
    registry = build_registry(cfg)
    return Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=StubAdapter({}),
        registry=registry,
        session_id="sess-exec-wall-budget",
    )


def _task(plan: Plan, task_id: str) -> Task:
    return next(t for ph in plan.phases for t in ph.tasks if t.id == task_id)


# Legal FSM chain from ``in_progress`` to ``complete`` (see
# ``orchestrator.task_state.TASK_TRANSITIONS``). A stubbed ``_execute_one``
# must walk it — ``in_progress -> complete`` directly is rejected by the
# transition validator.
_COMPLETE_CHAIN = (
    "coded",
    "auto_gated",
    "reviewed",
    "tested",
    "tournamented",
    "complete",
)


async def _drive_to_complete(pm: PlanManager, task_id: str) -> Task:
    """Stamp ``in_progress`` then walk the legal transition chain to
    ``complete`` — a stand-in for the real developer→review→test pipeline that
    keeps the DAG scheduler exercised for real without a live adapter."""
    await pm.update_task_status(task_id, "in_progress")
    task: Task | None = None
    for status in _COMPLETE_CHAIN:
        task = await pm.update_task_status(task_id, status)
    assert task is not None
    return task


# ══════════════════════════════════════════════════════════════════════════════
# (item a) Unit tests for the 3 new GuardrailEnforcer methods + the error type
# ══════════════════════════════════════════════════════════════════════════════


def test_error_is_autodev_not_tournament_and_carries_attrs() -> None:
    err = ExecutePhaseWallBudgetExceededError("msg", budget_s=1.5, elapsed_s=2.5)
    assert isinstance(err, AutodevError)
    # MUST NOT be a TournamentError — it fires even when no tournament runs.
    assert not isinstance(err, TournamentError)
    assert err.budget_s == 1.5
    assert err.elapsed_s == 2.5


def test_budget_unset_never_exceeded_even_with_huge_clock() -> None:
    enf = GuardrailEnforcer(_cfg(execute_phase_wall_budget_s=None))
    enf.start_execute_phase(clock=FakeClock(1_000_000.0))
    assert enf.execute_phase_wall_budget_exceeded() is False
    enf.check_execute_phase_wall_budget()  # no raise


def test_not_started_never_exceeded() -> None:
    # Budget set, but start_execute_phase never called → un-armed → never trips.
    enf = GuardrailEnforcer(_cfg(execute_phase_wall_budget_s=100.0))
    assert enf.execute_phase_wall_budget_exceeded() is False
    enf.check_execute_phase_wall_budget()  # no raise


def test_within_budget_does_not_trip() -> None:
    fake = FakeClock(0.0)
    enf = GuardrailEnforcer(_cfg(execute_phase_wall_budget_s=100.0))
    enf.start_execute_phase(clock=fake)
    fake.advance(50.0)
    assert enf.execute_phase_wall_budget_exceeded() is False
    enf.check_execute_phase_wall_budget()  # no raise


def test_exceeded_query_true_and_check_raises_with_attrs() -> None:
    fake = FakeClock(0.0)
    enf = GuardrailEnforcer(_cfg(execute_phase_wall_budget_s=100.0))
    enf.start_execute_phase(clock=fake)
    fake.advance(150.0)
    assert enf.execute_phase_wall_budget_exceeded() is True
    with pytest.raises(ExecutePhaseWallBudgetExceededError) as ei:
        enf.check_execute_phase_wall_budget()
    assert ei.value.budget_s == 100.0
    assert ei.value.elapsed_s == pytest.approx(150.0)


def test_start_execute_phase_resets_window_on_reentry() -> None:
    fake = FakeClock(0.0)
    enf = GuardrailEnforcer(_cfg(execute_phase_wall_budget_s=100.0))
    enf.start_execute_phase(clock=fake)
    fake.advance(150.0)
    assert enf.execute_phase_wall_budget_exceeded() is True
    # Re-arm with a fresh clock — a new invocation gets its own window.
    enf.start_execute_phase(clock=FakeClock(0.0))
    assert enf.execute_phase_wall_budget_exceeded() is False


def test_start_execute_phase_default_clock_is_cheap_when_unset() -> None:
    # Default clock (time.monotonic), unset budget → armed but never trips.
    enf = GuardrailEnforcer(_cfg())  # execute_phase_wall_budget_s defaults None
    enf.start_execute_phase()
    assert enf.execute_phase_wall_budget_exceeded() is False
    enf.check_execute_phase_wall_budget()  # no raise


def test_boundary_equal_budget_does_not_trip() -> None:
    # elapsed == budget is NOT a breach ("exceeded" is strictly greater).
    fake = FakeClock(0.0)
    enf = GuardrailEnforcer(_cfg(execute_phase_wall_budget_s=100.0))
    enf.start_execute_phase(clock=fake)
    fake.advance(100.0)
    assert enf.execute_phase_wall_budget_exceeded() is False
    enf.check_execute_phase_wall_budget()  # no raise


# ══════════════════════════════════════════════════════════════════════════════
# Ledger op registration (both the Literal alias + the _apply_op replay tuple)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_ledger_op_registered_and_replay_is_noop(tmp_path: Path) -> None:
    """The op validates on append (Literal alias) AND replays as a no-op
    (``_apply_op`` tuple) — driving the REAL append + REAL replay, not a
    hand-built entry."""
    pm = PlanManager(tmp_path, session_id="s")
    await pm.init_plan(_mk_single_task_plan())
    before = await pm.load()
    assert before is not None

    await pm.ledger_append(
        op="execute_phase_wall_budget_exceeded",
        payload={
            "budget_s": 1.0,
            "elapsed_s": 2.0,
            "tasks_processed": 0,
            "task_ids_processed": [],
            "reason": "wall budget exceeded",
        },
    )

    after = await pm.load()  # full replay through _apply_op — must not raise
    assert after is not None
    # Audit-only: plan state is byte-identical to before the op.
    assert [t.status for ph in after.phases for t in ph.tasks] == [
        t.status for ph in before.phases for t in ph.tasks
    ]
    ops = [e.op for e in await pm.read_ledger()]
    assert ops.count("execute_phase_wall_budget_exceeded") == 1


# ══════════════════════════════════════════════════════════════════════════════
# (item c + a) DAG integration: budget trips between task 1 and task 2
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_budget_trips_between_tasks_task2_never_dispatched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two serial tasks, budget 100s. Task 1.1 completes and advances the clock
    past the budget; the REAL ``_execute_phase_dag`` gate then refuses to spawn
    task 1.2 and the REAL enforcer raises. Assert: 1.2 was NEVER dispatched
    (stays ``pending``, not ``in_progress``), the error propagates, and the
    ledger carries the attributable op."""
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_two_task_plan())
    orch = _make_orch(tmp_path, execute_phase_wall_budget_s=100.0)

    fake = FakeClock(0.0)
    seed_calls: list[bool] = []

    def _seed(clock: Any = None) -> None:
        # Drive the REAL start_execute_phase, forcing the fake clock. Records
        # that run_execute_phase armed the budget (item a).
        seed_calls.append(True)
        GuardrailEnforcer.start_execute_phase(orch.guardrails, clock=fake)

    monkeypatch.setattr(orch.guardrails, "start_execute_phase", _seed)

    dispatched: list[str] = []

    async def _fake_execute_one(
        orch_arg: Any, task: Task, worktree_mgr: Any = None
    ) -> Task:
        dispatched.append(task.id)
        if task.id == "1.1":
            # Advance the clock past the budget WHILE task 1.1 runs.
            done = await _drive_to_complete(orch_arg.plan_manager, task.id)
            fake.advance(150.0)  # blow the 100s cumulative budget
            return done
        return await _drive_to_complete(orch_arg.plan_manager, task.id)

    monkeypatch.setattr(ep, "_execute_one", _fake_execute_one)

    with pytest.raises(ExecutePhaseWallBudgetExceededError):
        await ep.run_execute_phase(orch)

    assert seed_calls, "run_execute_phase must arm the execute-phase budget"
    assert dispatched == ["1.1"], (
        f"task 1.2 must never be dispatched once the budget is blown; "
        f"dispatched={dispatched}"
    )

    plan = await orch.plan_manager.load()
    assert plan is not None
    assert _task(plan, "1.1").status == "complete"
    # Never dispatched → never stamped in_progress → still pending.
    assert _task(plan, "1.2").status == "pending"

    ops = [e.op for e in await orch.plan_manager.read_ledger()]
    assert "execute_phase_wall_budget_exceeded" in ops
    breach = next(
        e
        for e in await orch.plan_manager.read_ledger()
        if e.op == "execute_phase_wall_budget_exceeded"
    )
    assert breach.payload["budget_s"] == 100.0
    assert breach.payload["reason"]
    # ``tasks_processed`` reflects ``run_execute_phase``'s run-level
    # ``processed`` accumulator at halt time. Here the breach fires from
    # ``_execute_phase_dag``'s internal raise (spec item c), which propagates
    # BEFORE the round loop folds the phase's partial results into
    # ``processed`` — so it is 0 (only tasks from fully-returned phases count).
    # The count is always internally consistent with the id list.
    assert breach.payload["tasks_processed"] == len(
        breach.payload["task_ids_processed"]
    )
    assert breach.payload["tasks_processed"] == 0
    assert breach.payload["task_ids_processed"] == []


# ══════════════════════════════════════════════════════════════════════════════
# (item b) budget trips mid a single task's retry loop → left NON-TERMINAL
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_budget_trips_after_phase1_before_phase2_reports_processed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two sequential phases. Phase 1's task completes (folded into the run's
    ``processed`` accumulator) and advances the clock past the budget; the
    post-DAG / pre-phase-review check (item d) then raises BEFORE phase 2
    starts. Exercises ``tasks_processed > 0`` attribution and confirms phase
    2's task is never dispatched."""
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_two_phase_plan())
    orch = _make_orch(tmp_path, execute_phase_wall_budget_s=100.0)

    fake = FakeClock(0.0)

    def _seed(clock: Any = None) -> None:
        GuardrailEnforcer.start_execute_phase(orch.guardrails, clock=fake)

    monkeypatch.setattr(orch.guardrails, "start_execute_phase", _seed)

    dispatched: list[str] = []

    async def _fake_execute_one(
        orch_arg: Any, task: Task, worktree_mgr: Any = None
    ) -> Task:
        dispatched.append(task.id)
        done = await _drive_to_complete(orch_arg.plan_manager, task.id)
        if task.id == "1.1":
            fake.advance(150.0)  # blow the budget as phase 1 finishes
        return done

    monkeypatch.setattr(ep, "_execute_one", _fake_execute_one)

    with pytest.raises(ExecutePhaseWallBudgetExceededError):
        await ep.run_execute_phase(orch)

    assert dispatched == ["1.1"], (
        f"phase 2's task must never be dispatched; dispatched={dispatched}"
    )
    plan = await orch.plan_manager.load()
    assert plan is not None
    assert _task(plan, "1.1").status == "complete"
    assert _task(plan, "2.1").status == "pending"

    breach = next(
        e
        for e in await orch.plan_manager.read_ledger()
        if e.op == "execute_phase_wall_budget_exceeded"
    )
    # Phase 1 fully returned before the breach, so its task IS folded into the
    # run-level ``processed`` accumulator the breadcrumb reports.
    assert breach.payload["tasks_processed"] == 1
    assert breach.payload["task_ids_processed"] == ["1.1"]


@pytest.mark.asyncio
async def test_cross_phase_dag_gates_and_raises_on_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opt-in cross-phase dispatcher (``cross_phase_parallelism_enabled``)
    mirrors the main-path gate+raise: once the budget is blown, no NEW worker
    spawns and the drained-with-pending-work branch raises. Two serial tasks;
    1.1 completes and blows the budget; 1.2 is never dispatched."""
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_two_task_plan())
    orch = _make_orch(tmp_path, execute_phase_wall_budget_s=100.0)
    orch.cfg.cross_phase_parallelism_enabled = True  # opt into the cross path

    fake = FakeClock(0.0)

    def _seed(clock: Any = None) -> None:
        GuardrailEnforcer.start_execute_phase(orch.guardrails, clock=fake)

    monkeypatch.setattr(orch.guardrails, "start_execute_phase", _seed)

    dispatched: list[str] = []

    async def _fake_execute_one(
        orch_arg: Any, task: Task, worktree_mgr: Any = None
    ) -> Task:
        dispatched.append(task.id)
        done = await _drive_to_complete(orch_arg.plan_manager, task.id)
        if task.id == "1.1":
            fake.advance(150.0)
        return done

    monkeypatch.setattr(ep, "_execute_one", _fake_execute_one)

    with pytest.raises(ExecutePhaseWallBudgetExceededError):
        await ep.run_execute_phase(orch)

    assert dispatched == ["1.1"], (
        f"cross-phase: task 1.2 must never be dispatched; dispatched={dispatched}"
    )
    plan = await orch.plan_manager.load()
    assert plan is not None
    assert _task(plan, "1.1").status == "complete"
    assert _task(plan, "1.2").status == "pending"
    ops = [e.op for e in await orch.plan_manager.read_ledger()]
    assert "execute_phase_wall_budget_exceeded" in ops


@pytest.mark.asyncio
async def test_budget_trips_mid_retry_leaves_task_non_terminal(
    tmp_path: Path,
) -> None:
    """Drive the REAL ``_execute_one`` with the budget pre-armed past its
    ceiling. The check at the TOP of the retry loop raises; the outer
    try/finally (``end_task`` + worktree cleanup only — no status mutation)
    leaves the task exactly as ``_execute_one`` stamped it: ``in_progress`` —
    NOT ``blocked`` / ``quarantined``."""
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_single_task_plan())
    orch = _make_orch(tmp_path, execute_phase_wall_budget_s=100.0)
    task = await orch.plan_manager.get_task("1.1")
    assert task is not None

    fake = FakeClock(0.0)
    orch.guardrails.start_execute_phase(clock=fake)
    fake.advance(150.0)  # already over budget when _execute_one's loop starts

    with pytest.raises(ExecutePhaseWallBudgetExceededError) as ei:
        await ep._execute_one(orch, task)
    assert ei.value.budget_s == 100.0
    assert ei.value.elapsed_s == pytest.approx(150.0)

    reloaded = await orch.plan_manager.get_task("1.1")
    assert reloaded is not None
    assert reloaded.status == "in_progress"
    assert reloaded.status not in ("blocked", "quarantined", "skipped", "complete")


@pytest.mark.asyncio
async def test_single_task_path_trips_after_completion_still_emits_breadcrumb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (code review, 2nd round): drive the REAL single-task branch
    of ``run_execute_phase(orch, task_id=...)`` — the ``autodev execute
    --task-id X`` / ``autodev resume`` (one in-progress task) shape — with the
    budget armed to blow exactly AFTER ``_execute_one`` completes the task but
    BEFORE ``_maybe_run_phase_review`` would start its own tournament.

    A first attempt at closing this checkpoint placed the check AFTER the
    try/except that owns this path, so the exception propagated UNCAUGHT and
    the ledger breadcrumb was silently skipped — the task's status was still
    safe, but the durable, greppable audit trail this whole feature exists to
    provide was missing on a genuinely reachable production path. This test
    pins the fix: the checkpoint must be INSIDE the existing try, routing
    through the existing ``except ExecutePhaseWallBudgetExceededError``
    handler like every other checkpoint."""
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_single_task_plan())
    orch = _make_orch(tmp_path, execute_phase_wall_budget_s=100.0)

    fake = FakeClock(0.0)

    def _seed(clock: Any = None) -> None:
        # run_execute_phase's OWN start_execute_phase() call (item a) would
        # otherwise re-arm with the real time.monotonic, wiping out the fake
        # clock — intercept it and force the fake clock through instead,
        # exactly as the whole-plan DAG test above does.
        GuardrailEnforcer.start_execute_phase(orch.guardrails, clock=fake)

    monkeypatch.setattr(orch.guardrails, "start_execute_phase", _seed)

    review_calls: list[str] = []

    async def _fake_execute_one(
        orch_arg: Any, task: Task, worktree_mgr: Any = None
    ) -> Task:
        done = await _drive_to_complete(orch_arg.plan_manager, task.id)
        fake.advance(150.0)  # blow the 100s budget right as the task finishes
        return done

    async def _fake_phase_review(*_a: Any, **_k: Any) -> None:
        review_calls.append("called")

    monkeypatch.setattr(ep, "_execute_one", _fake_execute_one)
    monkeypatch.setattr(ep, "_maybe_run_phase_review", _fake_phase_review)

    with pytest.raises(ExecutePhaseWallBudgetExceededError):
        await ep.run_execute_phase(orch, task_id="1.1")

    assert not review_calls, "phase-review must not start once budget is blown"

    plan = await orch.plan_manager.load()
    assert plan is not None
    # The task itself completed successfully — only phase-review was blocked.
    assert _task(plan, "1.1").status == "complete"

    ledger = await orch.plan_manager.read_ledger()
    ops = [e.op for e in ledger]
    assert "execute_phase_wall_budget_exceeded" in ops, (
        "the ledger breadcrumb must fire on the single-task path too, not "
        "just the whole-plan DAG loop"
    )
    breach = next(e for e in ledger if e.op == "execute_phase_wall_budget_exceeded")
    assert breach.payload["budget_s"] == 100.0
    assert breach.payload["reason"]
    # Unlike the whole-plan mid-DAG case (which reads 0), the just-completed
    # task IS folded into ``processed`` before this checkpoint fires.
    assert breach.payload["tasks_processed"] == 1
    assert breach.payload["task_ids_processed"] == ["1.1"]


# ══════════════════════════════════════════════════════════════════════════════
# (item d) None (default) = OFF = byte-identical legacy: no early stop
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_worker_reraises_budget_error_without_stamping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item (f): ``_execute_one_worker`` must re-raise
    ``ExecutePhaseWallBudgetExceededError`` UNCHANGED via its dedicated clause,
    NOT route it into the auth/infra handler (which stamps ``quarantined``) or
    the generic ``except Exception`` handler (which ``block_task``s + cascade-
    blocks). The error is produced by the REAL enforcer check (real budget +
    fake clock), not hand-constructed — this pins the handler's routing, which
    is exactly the "mirror a pattern without verifying it fits" hazard."""
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_single_task_plan())
    orch = _make_orch(tmp_path, execute_phase_wall_budget_s=100.0)
    task = await orch.plan_manager.get_task("1.1")
    assert task is not None

    fake = FakeClock(0.0)
    orch.guardrails.start_execute_phase(clock=fake)

    async def _raise_via_real_enforcer(
        orch_arg: Any, t: Task, worktree_mgr: Any = None
    ) -> Task:
        # Mirror the real _execute_one: stamp in_progress, then let the REAL
        # enforcer check raise once the budget is blown.
        await orch_arg.plan_manager.update_task_status(t.id, "in_progress")
        fake.advance(150.0)
        orch_arg.guardrails.check_execute_phase_wall_budget()  # raises for real
        return t  # pragma: no cover - unreachable

    monkeypatch.setattr(ep, "_execute_one", _raise_via_real_enforcer)

    with pytest.raises(ExecutePhaseWallBudgetExceededError):
        await ep._execute_one_worker(orch, task, None)

    reloaded = await orch.plan_manager.get_task("1.1")
    assert reloaded is not None
    # Left EXACTLY as-is (in_progress) — the worker did NOT quarantine or block.
    assert reloaded.status == "in_progress"
    assert reloaded.status not in ("blocked", "quarantined", "skipped", "complete")


@pytest.mark.asyncio
async def test_budget_none_never_stops_over_multitask_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Budget unset (default). A wildly-advancing clock over a multi-task plan
    must complete normally with ZERO early stop and NO ledger breach op."""
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_two_task_plan())
    orch = _make_orch(tmp_path, execute_phase_wall_budget_s=None)

    fake = FakeClock(0.0)

    def _seed(clock: Any = None) -> None:
        GuardrailEnforcer.start_execute_phase(orch.guardrails, clock=fake)

    monkeypatch.setattr(orch.guardrails, "start_execute_phase", _seed)

    async def _fake_execute_one(
        orch_arg: Any, task: Task, worktree_mgr: Any = None
    ) -> Task:
        fake.advance(1_000_000_000.0)  # must NOT trip: budget is OFF
        return await _drive_to_complete(orch_arg.plan_manager, task.id)

    monkeypatch.setattr(ep, "_execute_one", _fake_execute_one)

    # Must NOT raise.
    await ep.run_execute_phase(orch)

    plan = await orch.plan_manager.load()
    assert plan is not None
    assert all(t.status == "complete" for ph in plan.phases for t in ph.tasks)
    ops = [e.op for e in await orch.plan_manager.read_ledger()]
    assert "execute_phase_wall_budget_exceeded" not in ops


# ══════════════════════════════════════════════════════════════════════════════
# (item e) salvage: reap_orphans reverts the non-terminal task → completes next
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_salvage_reap_reverts_to_pending_and_completes_next_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The concrete proof that "no new salvage machinery needed" is TRUE, not
    asserted: force a real breach that leaves the task non-terminal, run the
    REAL ``reap_orphans`` sweep, confirm it reverts to ``pending``, then run a
    normal pass and confirm the task completes."""
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_single_task_plan())
    orch = _make_orch(tmp_path, execute_phase_wall_budget_s=100.0)
    task = await orch.plan_manager.get_task("1.1")
    assert task is not None

    # Phase 1: real breach mid-task via the real _execute_one (pre-armed).
    fake = FakeClock(0.0)
    orch.guardrails.start_execute_phase(clock=fake)
    fake.advance(150.0)
    with pytest.raises(ExecutePhaseWallBudgetExceededError):
        await ep._execute_one(orch, task)
    assert (await orch.plan_manager.get_task("1.1")).status == "in_progress"

    # Salvage step 1: the existing orphan-reap sweep reverts it to pending.
    reaped = await orch.plan_manager.reap_orphans()
    assert "1.1" in reaped
    assert (await orch.plan_manager.get_task("1.1")).status == "pending"

    # Salvage step 2: a normal pass (budget off) re-dispatches + completes it.
    orch.cfg.guardrails.execute_phase_wall_budget_s = None

    async def _complete(orch_arg: Any, t: Task, worktree_mgr: Any = None) -> Task:
        return await _drive_to_complete(orch_arg.plan_manager, t.id)

    monkeypatch.setattr(ep, "_execute_one", _complete)
    await ep.run_execute_phase(orch)
    assert (await orch.plan_manager.get_task("1.1")).status == "complete"


@pytest.mark.asyncio
async def test_run_execute_phase_reaps_in_progress_on_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end variant: after a breach leaves ``1.1`` in_progress, calling
    ``run_execute_phase`` AGAIN (budget off) reaps it at the top and completes
    it — proving the DAG-path reap (line ~3104) closes the loop without any
    manual ``reap_orphans`` call."""
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_single_task_plan())
    orch = _make_orch(tmp_path, execute_phase_wall_budget_s=100.0)
    task = await orch.plan_manager.get_task("1.1")
    assert task is not None

    fake = FakeClock(0.0)
    orch.guardrails.start_execute_phase(clock=fake)
    fake.advance(150.0)
    with pytest.raises(ExecutePhaseWallBudgetExceededError):
        await ep._execute_one(orch, task)
    assert (await orch.plan_manager.get_task("1.1")).status == "in_progress"

    orch.cfg.guardrails.execute_phase_wall_budget_s = None

    async def _complete(orch_arg: Any, t: Task, worktree_mgr: Any = None) -> Task:
        return await _drive_to_complete(orch_arg.plan_manager, t.id)

    monkeypatch.setattr(ep, "_execute_one", _complete)
    await ep.run_execute_phase(orch)  # reap_orphans at top reverts in_progress
    assert (await orch.plan_manager.get_task("1.1")).status == "complete"
