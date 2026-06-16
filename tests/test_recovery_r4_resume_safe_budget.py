"""Gate R4 (RECOVERY-CONTRACT §7 Step 2): resume-safe per-agent budget.

The ``BudgetEscalationTracker`` consecutive-``error_max_turns`` counter must
survive ``autodev resume`` (a fresh ``Orchestrator`` on the same cwd). Before
this fix the counter was an in-memory dict reconstructed EMPTY on every
construction, so a task that had already burned N consecutive
``error_max_turns`` cycles got the base budget AGAIN on resume instead of
escalating / exhausting.

These tests drive the counter up through the PRODUCTION ``delegate()`` path so
a ``budget_cycle`` op is persisted to a real ledger in a tmp cwd, then
construct a FRESH tracker (simulating resume) on the same cwd and assert the
attempt count is rehydrated (``> 0``), not reset to ``0``.

Anti-vacuity:

* A fresh tracker on a DIFFERENT (empty) cwd returns ``0`` — proving the
  ``> 0`` came from rehydration, not a constant.
* Disabling rehydration makes the resume assertion red (broken-control,
  exercised in the suite via monkeypatch in :func:`test_broken_control_*`).
* Reset semantics survive resume: persist attempt=2 then a reset (attempt=0),
  resume → ``current_attempt == 0`` (last-value-wins).
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator import execute_phase as ep
from orchestrator.budget_escalation import BudgetEscalationTracker
from orchestrator.delegation_envelope import DelegationEnvelope
from state.plan_manager import PlanManager
from state.schemas import AcceptanceCriterion, Phase, Plan, Task

from stub_adapter import StubAdapter


_SCOPE_TASK_ID = "1.1"
_ROLE = "developer"


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_single_task_plan(task_id: str = _SCOPE_TASK_ID) -> Plan:
    return Plan(
        plan_id="p-r4-resume-budget",
        spec_hash="0123456789abcdef",
        phases=[
            Phase(
                id="1",
                title="Setup",
                tasks=[
                    Task(
                        id=task_id,
                        phase_id="1",
                        title="t1",
                        description="d1",
                        complexity="simple",
                    ),
                ],
                acceptance=[AcceptanceCriterion(id="ph-1", description="ok")],
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
        complexity="simple",
    )


def _mk_developer_envelope(task_id: str = _SCOPE_TASK_ID) -> DelegationEnvelope:
    return DelegationEnvelope(
        task_id=task_id,
        target_agent=_ROLE,
        action="implement",
    )


def _mk_developer_task(task_id: str = _SCOPE_TASK_ID) -> Task:
    return Task(
        id=task_id,
        phase_id="1",
        title="t",
        description="d",
        complexity="simple",
    )


def _make_orch(cwd: Path, adapter: StubAdapter) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    cfg.tournaments.phase_review.enabled = False
    registry = build_registry(cfg)
    return Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-r4-resume-budget",
    )


async def _seed_plan(cwd: Path, plan: Plan) -> None:
    pm = PlanManager(cwd, session_id="sess-init")
    await pm.init_plan(plan)


def _max_turns_failure(text: str = "") -> Any:
    from adapters.types import AgentResult

    return AgentResult(
        success=False,
        text=text,
        duration_s=0.01,
        error="hit max turns",
        subtype="error_max_turns",
    )


def _success_result(text: str = "ok") -> Any:
    from adapters.types import AgentResult

    return AgentResult(
        success=True,
        text=text,
        duration_s=0.01,
        subtype="success",
    )


# ---------------------------------------------------------------------------
# Gate R4 — the resume-safety assertion (RED on HEAD before the fix).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r4_budget_counter_survives_resume(tmp_path: Path) -> None:
    """Drive two consecutive ``error_max_turns`` through the PRODUCTION
    ``delegate()`` path, then construct a FRESH tracker on the SAME cwd
    (simulating ``autodev resume``) and assert the attempt count is
    rehydrated to ``> 0``, NOT reset to ``0``.

    RED on HEAD: the in-memory tracker starts empty on every construction,
    so the fresh-tracker assertion reads ``0`` (today's reset bug).
    """
    await _seed_plan(tmp_path, _mk_single_task_plan())
    adapter = StubAdapter(
        {_ROLE: [_max_turns_failure(), _max_turns_failure()]}
    )
    orch = _make_orch(tmp_path, adapter)
    env = _mk_developer_envelope()
    task = _mk_developer_task()

    # Two consecutive max-turns dispatches via the real production path.
    # Each ``delegate`` calls ``record_result`` internally, which (post-fix)
    # persists a ``budget_cycle`` op to the on-disk ledger.
    await ep.delegate(orch, _ROLE, env, task=task)
    await ep.delegate(orch, _ROLE, env, task=task)

    # Sanity: the live tracker counted both cycles.
    assert (
        orch._budget_escalation_tracker.current_attempt(_SCOPE_TASK_ID, _ROLE)
        == 2
    )

    # Simulate ``autodev resume``: a FRESH tracker rehydrated from the SAME
    # ledger. Must NOT reset to 0.
    resumed = BudgetEscalationTracker.rehydrate_from_ledger(tmp_path)
    assert resumed.current_attempt(_SCOPE_TASK_ID, _ROLE) == 2


@pytest.mark.asyncio
async def test_r4_fresh_orchestrator_resume_rehydrates(tmp_path: Path) -> None:
    """End-to-end: a brand-new ``Orchestrator`` on the same cwd seeds its
    tracker from the ledger (the construction-time rehydration hook)."""
    await _seed_plan(tmp_path, _mk_single_task_plan())
    adapter = StubAdapter(
        {_ROLE: [_max_turns_failure(), _max_turns_failure()]}
    )
    orch = _make_orch(tmp_path, adapter)
    env = _mk_developer_envelope()
    task = _mk_developer_task()
    await ep.delegate(orch, _ROLE, env, task=task)
    await ep.delegate(orch, _ROLE, env, task=task)

    # Fresh Orchestrator on the SAME cwd → tracker rehydrated in __init__.
    resumed_adapter = StubAdapter({_ROLE: []})
    resumed_orch = _make_orch(tmp_path, resumed_adapter)
    assert (
        resumed_orch._budget_escalation_tracker.current_attempt(
            _SCOPE_TASK_ID, _ROLE
        )
        == 2
    )


def test_r4_anti_vacuity_empty_cwd_reads_zero(tmp_path: Path) -> None:
    """A fresh tracker on a DIFFERENT, empty cwd returns 0 — proves the
    ``> 0`` in the resume test came from rehydration, not a constant."""
    empty_cwd = tmp_path / "empty"
    empty_cwd.mkdir()
    fresh = BudgetEscalationTracker.rehydrate_from_ledger(empty_cwd)
    assert fresh.current_attempt(_SCOPE_TASK_ID, _ROLE) == 0


def test_r4_rehydrate_best_effort_missing_ledger(tmp_path: Path) -> None:
    """Rehydration is best-effort: a wholly missing ledger dir yields 0,
    never raises (mirrors ``count_prior_cycles`` except handling)."""
    missing = tmp_path / "does-not-exist"
    fresh = BudgetEscalationTracker.rehydrate_from_ledger(missing)
    assert fresh.current_attempt(_SCOPE_TASK_ID, _ROLE) == 0


@pytest.mark.asyncio
async def test_r4_reset_semantics_survive_resume(tmp_path: Path) -> None:
    """Last-value-wins: drive the counter up, then a SUCCESS (reset to 0)
    through the production path; resume must read 0, not the prior peak."""
    await _seed_plan(tmp_path, _mk_single_task_plan())
    adapter = StubAdapter(
        {
            _ROLE: [
                _max_turns_failure(),
                _max_turns_failure(),
                _success_result(),
            ]
        }
    )
    orch = _make_orch(tmp_path, adapter)
    env = _mk_developer_envelope()
    task = _mk_developer_task()

    await ep.delegate(orch, _ROLE, env, task=task)  # attempt -> 1
    await ep.delegate(orch, _ROLE, env, task=task)  # attempt -> 2
    await ep.delegate(orch, _ROLE, env, task=task)  # success -> reset 0

    assert (
        orch._budget_escalation_tracker.current_attempt(_SCOPE_TASK_ID, _ROLE)
        == 0
    )

    resumed = BudgetEscalationTracker.rehydrate_from_ledger(tmp_path)
    assert resumed.current_attempt(_SCOPE_TASK_ID, _ROLE) == 0


@pytest.mark.asyncio
async def test_r4_broken_control_no_rehydration_is_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Broken-control: with rehydration disabled (returns an empty tracker),
    the resume assertion goes red — proving the rehydration is load-bearing.
    """
    await _seed_plan(tmp_path, _mk_single_task_plan())
    adapter = StubAdapter(
        {_ROLE: [_max_turns_failure(), _max_turns_failure()]}
    )
    orch = _make_orch(tmp_path, adapter)
    env = _mk_developer_envelope()
    task = _mk_developer_task()
    await ep.delegate(orch, _ROLE, env, task=task)
    await ep.delegate(orch, _ROLE, env, task=task)

    # Disable rehydration: simulate the pre-fix construction.
    monkeypatch.setattr(
        BudgetEscalationTracker,
        "rehydrate_from_ledger",
        classmethod(lambda cls, cwd: cls()),
    )
    broken = BudgetEscalationTracker.rehydrate_from_ledger(tmp_path)
    assert broken.current_attempt(_SCOPE_TASK_ID, _ROLE) == 0
