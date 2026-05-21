"""v0.37.0 H2 integration: architect-refine cap is enforced end-to-end.

The motivating failure mode (from a recent stuck-recovery retrospective):
a single architect-refine response emitted 12 bullets, exploding a
phase's task list and burning hours of downstream developer time on
work the architect was meant to deliberate once. This test pins the
fix: a 12-bullet refine on a fresh phase lands at most ``cap`` tasks
(default 8), the ``corrective_cap_reached`` ledger op is observable
when the cap fires, and the originating task carries the
``user_decision_required`` recovery hint.

The architect-refine path is exercised by mocking the ``delegate``
symbol in :mod:`orchestrator.execute_phase` to return a crafted
``RESOLUTION: refine-tasks`` body — mirrors the H1 integration test's
shape so future refactors keep both tests in lockstep.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from adapters.types import AgentResult
from orchestrator import execute_phase as execute_phase_mod
from orchestrator.execute_phase import _dispatch_architect_consult
from state.plan_manager import PlanManager
from state.schemas import Phase, Plan, Task


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan() -> Plan:
    return Plan(
        plan_id="p-cap-int",
        spec_hash="0123456789abcdef",
        phases=[
            Phase(
                id="1",
                title="Investigate",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="failing task",
                        description="d",
                        complexity="medium",
                        status="in_progress",
                    ),
                ],
                baseline_commit="aaaa1111",
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
        complexity="medium",
    )


@dataclass
class _FakeCfg:
    """Mirrors the AutodevConfig surface that ``_dispatch_architect_consult``
    actually reads via ``getattr`` so we don't pull in the full config
    factory."""

    max_corrective_tasks_per_phase: int = 8
    corrective_cap_action: str = "soft_block_phase"
    # Disable the evidence-body threading helper to keep this test focused
    # on the cap path.
    recent_evidence_max_chars_per_kind: int = 0
    recent_evidence_include_kinds: list[str] = None  # type: ignore[assignment]


@dataclass
class _FakeOrch:
    cwd: Path
    cfg: _FakeCfg
    plan_manager: PlanManager


@pytest.mark.asyncio
async def test_architect_refine_caps_at_default_eight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 12-bullet architect-refine response lands at most 8 corrective
    tasks on a fresh phase, matching the default cap. The remaining 4
    bullets are dropped silently."""
    pm = PlanManager(tmp_path, session_id="sess-cap-int-1")
    await pm.init_plan(_mk_plan())

    task = Task(
        id="1.1",
        phase_id="1",
        title="failing task",
        description="d",
        complexity="medium",
        status="in_progress",
    )

    # Architect-refine body: 12 bullets.
    refine_body = (
        "RESOLUTION: refine-tasks\n"
        + "\n".join(f"- corrective bullet {i}" for i in range(1, 13))
        + "\n"
    )

    async def _fake_delegate(
        orch: Any,
        role: str,
        envelope: Any,
        extra_context: str = "",
        **kwargs: Any,
    ) -> AgentResult:
        return AgentResult(
            text=refine_body,
            success=True,
            files_changed=[],
            duration_s=0.1,
        )

    monkeypatch.setattr(execute_phase_mod, "delegate", _fake_delegate)

    @dataclass
    class _StuckState:
        discard_count: int = 1
        pivot_count: int = 0
        search_count: int = 0
        architect_count: int = 0
        last_event: str = "reviewer NEEDS_CHANGES"

    cfg = _FakeCfg(max_corrective_tasks_per_phase=8)
    cfg.recent_evidence_include_kinds = []
    orch = _FakeOrch(cwd=tmp_path, cfg=cfg, plan_manager=pm)

    await _dispatch_architect_consult(
        orch,  # type: ignore[arg-type]
        task,
        stuck_state=_StuckState(),
        reason="reviewer NEEDS_CHANGES",
        prior_attempts=None,
        web_context_block="",
    )

    plan = await pm.load()
    assert plan is not None
    phase = plan.phases[0]
    assert len(phase.corrective_task_ids) == 8, (
        f"expected at most 8 corrective tasks, got "
        f"{len(phase.corrective_task_ids)}"
    )
    # The base task plus the 8 corrective tasks land on the phase.
    assert len(phase.tasks) == 1 + 8


@pytest.mark.asyncio
async def test_architect_refine_cap_reached_soft_blocks_originating_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the phase has already reached the cap, a fresh architect-
    refine call short-circuits BEFORE invoking the parser:
    ``corrective_cap_reached`` lands in the ledger and the originating
    task is marked ``blocked`` with a ``user_decision_required``
    recovery hint."""
    pm = PlanManager(tmp_path, session_id="sess-cap-int-2")
    await pm.init_plan(_mk_plan())

    # Pre-fill the phase to the cap so the next refine hits budget=0.
    pre_existing = [
        Task(
            id=f"1.c{i}",
            phase_id="1",
            title=f"prior corrective {i}",
            description="d",
            complexity="medium",
            assigned_agent="developer",
            status="complete",
        )
        for i in range(1, 4)
    ]
    await pm.append_corrective_tasks("1", pre_existing)

    task = Task(
        id="1.1",
        phase_id="1",
        title="failing task",
        description="d",
        complexity="medium",
        status="in_progress",
    )

    # Architect would emit a refine — but the cap path must fire first.
    refine_body = (
        "RESOLUTION: refine-tasks\n"
        "- never-lands-1\n"
        "- never-lands-2\n"
    )

    delegate_calls: list[str] = []

    async def _fake_delegate(
        orch: Any,
        role: str,
        envelope: Any,
        extra_context: str = "",
        **kwargs: Any,
    ) -> AgentResult:
        delegate_calls.append(role)
        return AgentResult(
            text=refine_body,
            success=True,
            files_changed=[],
            duration_s=0.1,
        )

    monkeypatch.setattr(execute_phase_mod, "delegate", _fake_delegate)

    @dataclass
    class _StuckState:
        discard_count: int = 1
        pivot_count: int = 0
        search_count: int = 0
        architect_count: int = 0
        last_event: str = "reviewer NEEDS_CHANGES"

    cfg = _FakeCfg(max_corrective_tasks_per_phase=3)
    cfg.recent_evidence_include_kinds = []
    orch = _FakeOrch(cwd=tmp_path, cfg=cfg, plan_manager=pm)

    result = await _dispatch_architect_consult(
        orch,  # type: ignore[arg-type]
        task,
        stuck_state=_StuckState(),
        reason="reviewer NEEDS_CHANGES",
        prior_attempts=None,
        web_context_block="",
    )

    # The cap-hit path must NOT inject any new corrective tasks.
    plan = await pm.load()
    assert plan is not None
    assert len(plan.phases[0].corrective_task_ids) == 3

    # The originating task is soft-blocked with a recovery hint.
    # ``blocked_reason`` and ``recovery_hint`` are first-class Task fields
    # (the ``update_task_status`` ``meta=`` kwarg copies them off the
    # metadata dict before persisting).
    assert result is not None
    assert result.status == "blocked"
    assert "corrective_cap_reached" in (result.blocked_reason or "")
    assert result.recovery_hint is not None
    assert result.recovery_hint.class_ == "user_decision_required"
    # The hint surfaces the requeue + rewind copy-paste commands.
    commands = result.recovery_hint.commands_to_try
    assert any("autodev requeue --task 1.1" in c for c in commands)
    assert any("autodev rewind --to-phase 1" in c for c in commands)

    # Ledger fired the cap op (alongside the architect_consult op).
    entries = await pm.read_ledger()
    cap_ops = [e for e in entries if e.op == "corrective_cap_reached"]
    assert len(cap_ops) >= 1
    payload = cap_ops[-1].payload
    assert payload["phase_id"] == "1"
    assert payload["task_id"] == "1.1"
    assert payload["cap"] == 3
    assert payload["site"] == "architect_refine"
