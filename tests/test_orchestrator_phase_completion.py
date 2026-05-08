"""Tests for v0.9.0 phase-completion detection in :mod:`execute_phase`.

The integration target: ``run_execute_phase`` observes phase boundaries
and triggers ``_run_phase_review`` once per phase. We don't actually run
the developer / reviewer / test_engineer pipeline here — those tests
live elsewhere — instead we monkey-patch ``_execute_one`` and
``run_phase_review_tournament`` so we can observe the orchestrator's
phase-boundary detection and outcome-application logic in isolation.
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
from orchestrator import phase_review_runner as prr
from orchestrator.phase_review_runner import PhaseReviewOutcome
from state.plan_manager import PlanManager
from state.schemas import AcceptanceCriterion, Phase, Plan, Task

from stub_adapter import StubAdapter, ok


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_two_phase_plan() -> Plan:
    """Two phases, two tasks each. All tasks start ``pending``."""
    return Plan(
        plan_id="p-test",
        spec_hash="0123456789abcdef",
        phases=[
            Phase(
                id="1",
                title="Investigate",
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
                    ),
                ],
                acceptance=[
                    AcceptanceCriterion(id="ph-ac-1", description="all good")
                ],
            ),
            Phase(
                id="2",
                title="Finalize",
                tasks=[
                    Task(
                        id="2.1",
                        phase_id="2",
                        title="t3",
                        description="d3",
                        complexity="medium",
                    ),
                    Task(
                        id="2.2",
                        phase_id="2",
                        title="t4",
                        description="d4",
                        complexity="medium",
                    ),
                ],
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
        complexity="medium",
    )


def _make_orch(
    cwd: Path, *, phase_review_enabled: bool = True
) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.phase_review.enabled = phase_review_enabled
    cfg.tournaments.auto_disable_for_models = []
    cfg.tournaments.impl.enabled = False
    cfg.tournaments.plan.enabled = False
    registry = build_registry(cfg)
    adapter = StubAdapter({"explorer": ok("ok")})
    return Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-test-phase-completion",
    )


def _patch_execute_one(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace ``_execute_one`` with a fake that marks the task complete.

    Returns the captured list of task ids in order. We walk the FSM
    transitions ``pending → in_progress → complete`` (mirroring the real
    ``_execute_one`` short circuit at terminal) so the strict transition
    validator doesn't reject the move.
    """
    captured: list[str] = []

    async def fake_execute_one(
        orch: Any, task: Task, worktree_mgr: Any = None
    ) -> Task:
        captured.append(task.id)
        # Walk the full FSM path so the strict transition validator
        # accepts each step.
        for status in (
            "in_progress",
            "coded",
            "auto_gated",
            "reviewed",
            "tested",
            "tournamented",
            "complete",
        ):
            await orch.plan_manager.update_task_status(task.id, status)
        # Re-fetch the persisted task so the caller sees status=complete.
        return (await orch.plan_manager.get_task(task.id)) or task

    monkeypatch.setattr(ep, "_execute_one", fake_execute_one)
    return captured


def _patch_phase_review(
    monkeypatch: pytest.MonkeyPatch,
    *,
    winner: str = "A",
    direction: str = "",
) -> list[str]:
    """Replace ``run_phase_review_tournament`` with a fake outcome.

    Returns a list of phase ids that were observed by the fake (so tests
    can assert "the tournament fired for phase 1 but not phase 2").
    """
    fired_for: list[str] = []

    async def fake_run(orch, phase, baseline_commit, tip_commit, spec_md):
        fired_for.append(phase.id)
        return PhaseReviewOutcome(
            winner=winner,  # type: ignore[arg-type]
            accept_phase=(winner == "A"),
            corrective_direction=direction if winner != "A" else None,
            history=[],
        )

    monkeypatch.setattr(
        ep,
        "run_phase_review_tournament",
        fake_run,
        raising=False,
    )
    # The execute_phase module imports the function lazily inside
    # _run_phase_review, so also patch the source module so that the
    # late import resolves to the fake.
    monkeypatch.setattr(prr, "run_phase_review_tournament", fake_run)
    return fired_for


# ---------------------------------------------------------------------------
# Phase boundary detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase_boundary_detected_on_last_task_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When phase 1's last task completes, the review fires before phase 2."""
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_two_phase_plan())
    orch = _make_orch(tmp_path)
    captured = _patch_execute_one(monkeypatch)
    fired_for = _patch_phase_review(monkeypatch, winner="A")

    await ep.run_execute_phase(orch)

    # All four tasks ran in order.
    assert captured == ["1.1", "1.2", "2.1", "2.2"]
    # The review fired for both phases (once each).
    assert fired_for == ["1", "2"]


@pytest.mark.asyncio
async def test_baseline_commit_recorded_at_phase_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``baseline_commit`` is captured at first entry to each phase."""
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_two_phase_plan())
    orch = _make_orch(tmp_path)
    _patch_execute_one(monkeypatch)
    _patch_phase_review(monkeypatch, winner="A")

    # Stub _git_rev_parse_head so we get a deterministic value.
    monkeypatch.setattr(ep, "_git_rev_parse_head", lambda cwd: "abc123")

    await ep.run_execute_phase(orch)
    plan = await orch.plan_manager.load()
    assert plan is not None
    assert plan.phases[0].baseline_commit == "abc123"
    assert plan.phases[1].baseline_commit == "abc123"


# ---------------------------------------------------------------------------
# Toggling and outcome application
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase_review_skipped_when_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cfg.tournaments.phase_review.enabled=False`` short-circuits."""
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_two_phase_plan())
    orch = _make_orch(tmp_path, phase_review_enabled=False)
    _patch_execute_one(monkeypatch)
    fired_for = _patch_phase_review(monkeypatch, winner="A")

    await ep.run_execute_phase(orch)
    # No review fired for either phase.
    assert fired_for == []


@pytest.mark.asyncio
async def test_phase_review_a_winner_marks_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_two_phase_plan())
    orch = _make_orch(tmp_path)
    _patch_execute_one(monkeypatch)
    _patch_phase_review(monkeypatch, winner="A")

    await ep.run_execute_phase(orch)
    plan = await orch.plan_manager.load()
    assert plan is not None
    assert plan.phases[0].review_status == "accepted"
    assert plan.phases[1].review_status == "accepted"


@pytest.mark.asyncio
async def test_phase_review_b_winner_injects_corrective_tasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_two_phase_plan())
    orch = _make_orch(tmp_path)
    _patch_execute_one(monkeypatch)
    # Phase 1 review returns B with two corrective bullets;
    # phase 2 review returns A.
    state = {"calls": 0}

    async def varied_outcome(orch_, phase, b, t, s):
        state["calls"] += 1
        if phase.id == "1":
            return PhaseReviewOutcome(
                winner="B",
                accept_phase=False,
                corrective_direction=(
                    "- Add coverage for the dispatcher\n"
                    "- Document the queue contract\n"
                ),
                history=[],
            )
        return PhaseReviewOutcome(
            winner="A", accept_phase=True, corrective_direction=None, history=[]
        )

    monkeypatch.setattr(prr, "run_phase_review_tournament", varied_outcome)

    await ep.run_execute_phase(orch)
    plan = await orch.plan_manager.load()
    assert plan is not None
    # Phase 1 received corrective tasks.
    phase1 = plan.phases[0]
    corrective_ids = phase1.corrective_task_ids
    assert len(corrective_ids) == 2
    assert all(cid.startswith("1.c") for cid in corrective_ids)
    # The corrective tasks were ALSO appended to the phase's task list.
    appended = [t for t in phase1.tasks if t.id in corrective_ids]
    assert len(appended) == 2


@pytest.mark.asyncio
async def test_phase_review_ab_winner_injects_corrective_tasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_two_phase_plan())
    orch = _make_orch(tmp_path)
    _patch_execute_one(monkeypatch)

    async def ab_outcome(orch_, phase, b, t, s):
        if phase.id == "1":
            return PhaseReviewOutcome(
                winner="AB",
                accept_phase=False,
                corrective_direction="- AB merged correction\n",
                history=[],
            )
        return PhaseReviewOutcome(
            winner="A", accept_phase=True, corrective_direction=None, history=[]
        )

    monkeypatch.setattr(prr, "run_phase_review_tournament", ab_outcome)

    await ep.run_execute_phase(orch)
    plan = await orch.plan_manager.load()
    assert plan is not None
    assert len(plan.phases[0].corrective_task_ids) == 1


@pytest.mark.asyncio
async def test_phase_review_exception_sets_skipped_and_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_two_phase_plan())
    orch = _make_orch(tmp_path)
    _patch_execute_one(monkeypatch)

    async def failing_review(orch_, phase, b, t, s):
        if phase.id == "1":
            raise RuntimeError("simulated phase-review failure")
        return PhaseReviewOutcome(
            winner="A", accept_phase=True, corrective_direction=None, history=[]
        )

    monkeypatch.setattr(prr, "run_phase_review_tournament", failing_review)

    # Must not raise — failure is logged and the loop proceeds.
    await ep.run_execute_phase(orch)
    plan = await orch.plan_manager.load()
    assert plan is not None
    assert plan.phases[0].review_status == "skipped"
    assert plan.phases[1].review_status == "accepted"


# ---------------------------------------------------------------------------
# Critical loop guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_corrective_tasks_do_not_re_trigger_phase_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The critical loop guard: corrective tasks land terminal → review
    transitions ``corrective_required`` → ``accepted`` directly without
    re-firing the tournament."""
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_two_phase_plan())
    orch = _make_orch(tmp_path)
    _patch_execute_one(monkeypatch)

    review_fire_count = {"phase_1": 0, "phase_2": 0}

    async def review_with_corrective_for_phase_1(orch_, phase, b, t, s):
        if phase.id == "1":
            review_fire_count["phase_1"] += 1
            return PhaseReviewOutcome(
                winner="B",
                accept_phase=False,
                corrective_direction="- corrective\n",
                history=[],
            )
        review_fire_count["phase_2"] += 1
        return PhaseReviewOutcome(
            winner="A", accept_phase=True, corrective_direction=None, history=[]
        )

    monkeypatch.setattr(
        prr, "run_phase_review_tournament", review_with_corrective_for_phase_1
    )

    await ep.run_execute_phase(orch)
    # Phase 1 review fires exactly once even though corrective tasks
    # subsequently completed.
    assert review_fire_count["phase_1"] == 1
    # Phase 2 review fires exactly once.
    assert review_fire_count["phase_2"] == 1
    plan = await orch.plan_manager.load()
    assert plan is not None
    # Phase 1 transitions corrective_required → accepted directly.
    assert plan.phases[0].review_status == "accepted"


@pytest.mark.asyncio
async def test_phase_review_runs_only_once_per_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-running ``run_execute_phase`` after acceptance does not refire."""
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_two_phase_plan())
    orch = _make_orch(tmp_path)
    _patch_execute_one(monkeypatch)
    fired_for = _patch_phase_review(monkeypatch, winner="A")

    await ep.run_execute_phase(orch)
    # Second call: all tasks already complete → next_pending_task is None
    # immediately → no tournament fires.
    fired_for.clear()
    await ep.run_execute_phase(orch)
    assert fired_for == []
