"""v0.37.0 H2: per-phase corrective-task cap, cumulative across rounds.

These tests exercise :func:`orchestrator.execute_phase._run_phase_review`
end-to-end against a real :class:`PlanManager` on a tmp_path. The
phase-review tournament is mocked at the
:func:`orchestrator.phase_review_runner.run_phase_review_tournament` symbol
to return crafted B-winner outcomes whose corrective-direction bullet
lists drive the cap path.

Pinned because plan inflation observed in real-world runs can stack
corrective rounds: a phase's existing ``corrective_task_ids`` plus a new
round's parsed bullets must NEVER exceed ``cfg.max_corrective_tasks_per_phase``.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from orchestrator import execute_phase as execute_phase_mod
from orchestrator import phase_review_runner as prr
from orchestrator.execute_phase import _run_phase_review
from orchestrator.phase_review_runner import PhaseReviewOutcome
from state.plan_manager import PlanManager
from state.schemas import Phase, Plan, Task


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan() -> Plan:
    return Plan(
        plan_id="p-cap",
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
                baseline_commit="aaaa1111",
                end_checkpoint_commit="bbbb2222",
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
        complexity="medium",
    )


@dataclass
class _FakeCfg:
    """Minimal cfg surface used by ``_run_phase_review``."""

    max_corrective_tasks_per_phase: int = 5
    corrective_cap_action: str = "soft_block_phase"


@dataclass
class _FakeOrch:
    cwd: Path
    cfg: _FakeCfg
    plan_manager: PlanManager
    _spec_md: str = ""

    @property
    def adapter(self) -> Any:  # noqa: D401 — protocol-level placeholder
        return None


def _outcome(direction: str) -> PhaseReviewOutcome:
    return PhaseReviewOutcome(
        winner="B",
        accept_phase=False,
        corrective_direction=direction,
        history=[],
    )


@pytest.mark.asyncio
async def test_corrective_cap_truncates_within_a_single_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single B-winner round whose direction text has 12 bullets lands
    at most ``cap`` corrective tasks. The remaining bullets are dropped
    silently (logged via ``corrective_parser.parsed.dropped``)."""
    pm = PlanManager(tmp_path, session_id="sess-cap-1")
    await pm.init_plan(_mk_plan())

    cap = 5
    direction = "\n".join(f"- bullet {i}" for i in range(1, 13))

    async def _fake_run(
        orch: Any, phase: Phase, baseline: str, tip: str, spec_md: str
    ) -> PhaseReviewOutcome:
        return _outcome(direction)

    monkeypatch.setattr(prr, "run_phase_review_tournament", _fake_run)

    orch = _FakeOrch(cwd=tmp_path, cfg=_FakeCfg(max_corrective_tasks_per_phase=cap), plan_manager=pm)
    plan = await pm.load()
    assert plan is not None
    phase = plan.phases[0]

    await _run_phase_review(orch, phase)  # type: ignore[arg-type]

    plan = await pm.load()
    assert plan is not None
    phase = plan.phases[0]
    assert len(phase.corrective_task_ids) == cap
    assert phase.review_status == "corrective_required"


@pytest.mark.asyncio
async def test_corrective_cap_is_cumulative_across_two_rounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 1 lands 3 corrective tasks; round 2's 5-bullet direction
    resolves to only 2 because the phase has 3 remaining of the 5-task
    budget. Cumulative invariant holds."""
    pm = PlanManager(tmp_path, session_id="sess-cap-2")
    await pm.init_plan(_mk_plan())

    cap = 5
    orch = _FakeOrch(cwd=tmp_path, cfg=_FakeCfg(max_corrective_tasks_per_phase=cap), plan_manager=pm)

    # Round 1: 3 bullets.
    direction_r1 = "- a1\n- a2\n- a3\n"

    async def _fake_run_r1(orch: Any, phase: Phase, b: str, t: str, s: str) -> PhaseReviewOutcome:
        return _outcome(direction_r1)

    monkeypatch.setattr(prr, "run_phase_review_tournament", _fake_run_r1)

    plan = await pm.load()
    assert plan is not None
    await _run_phase_review(orch, plan.phases[0])  # type: ignore[arg-type]

    plan = await pm.load()
    assert plan is not None
    assert len(plan.phases[0].corrective_task_ids) == 3

    # Round 2: 5 bullets — only 2 should land (budget = 5 - 3 = 2).
    # Reset review_status so the runner can re-fire.
    await pm.update_phase_meta(plan.phases[0].id, review_status="in_progress")
    direction_r2 = "- b1\n- b2\n- b3\n- b4\n- b5\n"

    async def _fake_run_r2(orch: Any, phase: Phase, b: str, t: str, s: str) -> PhaseReviewOutcome:
        return _outcome(direction_r2)

    monkeypatch.setattr(prr, "run_phase_review_tournament", _fake_run_r2)

    plan = await pm.load()
    assert plan is not None
    await _run_phase_review(orch, plan.phases[0])  # type: ignore[arg-type]

    plan = await pm.load()
    assert plan is not None
    assert len(plan.phases[0].corrective_task_ids) == 5  # 3 + 2 = cap
    # Still within budget so review_status remains "corrective_required"
    # for the executor to pick up the new sub-tasks.
    assert plan.phases[0].review_status == "corrective_required"


@pytest.mark.asyncio
async def test_corrective_cap_third_round_fires_capped_when_budget_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the phase has hit the cap, a subsequent B-winner tournament
    refuses to spawn more tasks: ``review_status`` flips to ``"capped"``
    and a ``corrective_cap_reached`` ledger op fires."""
    pm = PlanManager(tmp_path, session_id="sess-cap-3")
    await pm.init_plan(_mk_plan())

    cap = 3
    orch = _FakeOrch(cwd=tmp_path, cfg=_FakeCfg(max_corrective_tasks_per_phase=cap), plan_manager=pm)

    # Round 1 fills the budget exactly.
    direction_r1 = "- a1\n- a2\n- a3\n"

    async def _fake_run_r1(orch: Any, phase: Phase, b: str, t: str, s: str) -> PhaseReviewOutcome:
        return _outcome(direction_r1)

    monkeypatch.setattr(prr, "run_phase_review_tournament", _fake_run_r1)
    plan = await pm.load()
    assert plan is not None
    await _run_phase_review(orch, plan.phases[0])  # type: ignore[arg-type]

    plan = await pm.load()
    assert plan is not None
    assert len(plan.phases[0].corrective_task_ids) == cap

    # Round 2: budget = 0 → cap-reached path fires.
    await pm.update_phase_meta(plan.phases[0].id, review_status="in_progress")

    async def _fake_run_r2(orch: Any, phase: Phase, b: str, t: str, s: str) -> PhaseReviewOutcome:
        return _outcome("- never-lands-1\n- never-lands-2\n")

    monkeypatch.setattr(prr, "run_phase_review_tournament", _fake_run_r2)
    plan = await pm.load()
    assert plan is not None
    await _run_phase_review(orch, plan.phases[0])  # type: ignore[arg-type]

    plan = await pm.load()
    assert plan is not None
    assert len(plan.phases[0].corrective_task_ids) == cap  # unchanged
    assert plan.phases[0].review_status == "capped"

    # Verify the ledger op fired.
    entries = await pm.read_ledger()
    cap_ops = [e for e in entries if e.op == "corrective_cap_reached"]
    assert len(cap_ops) >= 1
    payload = cap_ops[-1].payload
    assert payload["phase_id"] == "1"
    assert payload["cap"] == cap
    assert payload["site"] == "phase_review"
