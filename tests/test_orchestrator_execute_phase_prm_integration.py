"""v0.15.0: PRM trajectory integration with execute_phase delegate dispatch.

Validates the end-to-end wiring between the in-memory
:class:`orchestrator.prm.TrajectoryStore` (held on the Orchestrator)
and :func:`orchestrator.execute_phase.delegate`:

* Before each delegate call, a :class:`TrajectoryEvent` is recorded.
* After each delegate call, ``store.analyze(task_id)`` is consulted.
* If a pattern fires AND no correction has been emitted for the task
  yet, a :class:`CourseCorrection` is attached to the orchestrator's
  per-task pending-correction state so the NEXT delegate call splices
  it into the prompt.
* A ``course_correction_emitted`` ledger op is appended on first emit.

The integration is tested via a minimal scenario: 3 identical developer
dispatches → ``repetition_loop`` pattern → CourseCorrection emitted →
4th dispatch's prompt contains the markdown block.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

import pytest

from orchestrator import execute_phase as ep
from orchestrator.prm import (
    TrajectoryStore,
)
from state.plan_manager import PlanManager
from state.schemas import Phase, Plan, Task


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan(tasks: list[Task]) -> Plan:
    return Plan(
        plan_id="p-prm",
        spec_hash="cafe",
        phases=[Phase(id="1", title="prm", tasks=tasks)],
        created_at=_iso(),
        updated_at=_iso(),
    )


def _make_orch(tmp_path: Path, pm: PlanManager) -> Any:
    from config.defaults import default_config

    cfg = default_config()
    cfg.tournaments.execute_max_parallel_tasks = 1
    cfg.tournaments.phase_review.enabled = False

    captured: dict = {"prompts": []}

    class FakeAdapter:
        async def execute(self, inv):
            captured["prompts"].append(inv.prompt)
            from adapters.types import AgentResult

            return AgentResult(
                success=False,
                text="dispatch output\n",
                duration_s=0.01,
                files_changed=[],
                diff="",
            )

    class FakeRegistry:
        def get(self, role):
            from adapters.types import AgentSpec

            return AgentSpec(
                name=role,
                model="sonnet",
                prompt="developer prompt",
                description="",
                tools=[],
                max_turns=1,
            )

    class FakeKnowledge:
        async def inject_block(self, role, task_id=None):
            return ""

        async def record_tournament_event(self, event):
            return None

    class FakeGuard:
        def start_execute_phase(self, *a, **k):
            return None

        def execute_phase_wall_budget_exceeded(self, *a, **k):
            return False

        def check_execute_phase_wall_budget(self, *a, **k):
            return None

        def start_task(self, tid):
            pass

        def end_task(self, tid):
            pass

        def pre_invocation(self, *a, **kw):
            pass

        def post_invocation(self, *a, **kw):
            pass

    class FakeLoop:
        def observe(self, *a, **kw):
            pass

    orch = type(
        "Orch",
        (),
        {
            "cwd": tmp_path,
            "session_id": "test",
            "plan_manager": pm,
            "cfg": cfg,
            "guardrails": FakeGuard(),
            "adapter": FakeAdapter(),
            "registry": FakeRegistry(),
            "knowledge": FakeKnowledge(),
            "loop_detector": FakeLoop(),
            "plugin_registry": None,
            "disable_impl_tournament": True,
            "_captured": captured,
            "trajectory_store": TrajectoryStore(),
        },
    )()
    return orch


@pytest.mark.asyncio
async def test_repetition_loop_pattern_emits_course_correction_into_next_prompt(
    tmp_path: Path,
) -> None:
    """3 identical developer dispatches → ``repetition_loop`` detected →
    4th dispatch's prompt contains the COURSE CORRECTION markdown block.
    """
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan(
            [
                Task(
                    id="1.1",
                    phase_id="1",
                    title="t",
                    description="d",
                    files=["src/foo.py"],
                )
            ]
        )
    )
    orch = _make_orch(tmp_path, pm)
    task = (await pm.get_task("1.1")) or pytest.fail("task not found")

    # Dispatch the developer 4 times — first 3 prime the repetition_loop
    # detector (3 identical events), the 4th must see the COURSE
    # CORRECTION block injected into its prompt.
    from orchestrator.delegation_envelope import DelegationEnvelope

    env = DelegationEnvelope(
        task_id=task.id,
        target_agent="developer",
        action="implement",
        files=["src/foo.py"],
    )
    for _ in range(4):
        await ep.delegate(orch, "developer", env, task=task)

    prompts = orch._captured["prompts"]
    assert len(prompts) == 4
    # The first 3 prompts must NOT contain the COURSE CORRECTION block.
    for i in range(3):
        assert "## COURSE CORRECTION" not in prompts[i]
    # The 4th prompt MUST contain the COURSE CORRECTION block.
    assert "## COURSE CORRECTION" in prompts[3]
    assert "repetition_loop" in prompts[3]
    assert "reasoning_error" in prompts[3]


@pytest.mark.asyncio
async def test_course_correction_emitted_only_once_per_task(
    tmp_path: Path,
) -> None:
    """Once a correction has been emitted, subsequent dispatches with the
    same pattern fingerprint must NOT re-emit (the contract caps to ONE
    correction per task per fingerprint)."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan(
            [
                Task(
                    id="1.1",
                    phase_id="1",
                    title="t",
                    description="d",
                    files=["src/foo.py"],
                )
            ]
        )
    )
    orch = _make_orch(tmp_path, pm)
    task = (await pm.get_task("1.1")) or pytest.fail("task not found")

    from orchestrator.delegation_envelope import DelegationEnvelope

    env = DelegationEnvelope(
        task_id=task.id,
        target_agent="developer",
        action="implement",
        files=["src/foo.py"],
    )
    for _ in range(6):
        await ep.delegate(orch, "developer", env, task=task)

    prompts = orch._captured["prompts"]
    # The 4th, 5th, 6th prompts after pattern detection — only the FIRST
    # one (#4) carries the COURSE CORRECTION block; #5 and #6 do NOT
    # repeat it.
    assert "## COURSE CORRECTION" in prompts[3]
    # Re-emission contract: subsequent prompts do not repeat the block.
    correction_count = sum(
        1 for p in prompts if "## COURSE CORRECTION" in p
    )
    assert correction_count == 1, (
        f"expected exactly 1 emitted correction; got {correction_count}"
    )
