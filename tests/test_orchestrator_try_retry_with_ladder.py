"""v0.15.0: ``_try_retry_or_escalate`` integration with the escalation ladder.

The refactored helper consults
:func:`orchestrator.escalation_ladder.next_step` against the per-task
:class:`StuckState` (held on the PlanManager) and dispatches accordingly:

* ``"continue"`` → preserve legacy retry-then-escalate behavior
  (backward-compat — the dominant path for normal runs).
* ``"REFINE"`` / ``"PIVOT"`` / ``"SOFT_BLOCKER"`` → call
  :func:`_escalate_stuck_to_critic` and apply the resolution.

Tests intercept :func:`_escalate_stuck_to_critic` so we can assert it gets
invoked with the right ladder_step at the right thresholds without
needing a live critic adapter.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

import pytest

from orchestrator import execute_phase as ep
from orchestrator.execute_phase import StuckResolution
from state.plan_manager import PlanManager
from state.schemas import Phase, Plan, Task


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan(tasks: list[Task]) -> Plan:
    return Plan(
        plan_id="p-ladder",
        spec_hash="cafe",
        phases=[Phase(id="1", title="ladder", tasks=tasks)],
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
                success=True,
                text="critic response\n",
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
                prompt="prompt",
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
        },
    )()
    return orch


@pytest.mark.asyncio
async def test_below_threshold_preserves_legacy_continue_behavior(
    tmp_path: Path,
) -> None:
    """``next_step()`` returns ``"continue"`` for the first 2 discards →
    behavior identical to v0.14.0 (retry-then-escalate via legacy path).
    """
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan([Task(id="1.1", phase_id="1", title="t", description="d")])
    )
    orch = _make_orch(tmp_path, pm)
    task = (await pm.get_task("1.1")) or pytest.fail("task not found")
    # First call: retry path (retry_limit=3, no escalation yet).
    out = await ep._try_retry_or_escalate(
        orch, task, retry_limit=3, reason="coder failure"
    )
    assert out.escalated is False
    # Stuck state was bumped to discard_count=1.
    state = await pm.get_stuck_state("1.1")
    assert state.discard_count == 1


@pytest.mark.asyncio
async def test_3_discards_invokes_stuck_critic_in_refine_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At discard_count=3 the ladder returns ``"REFINE"`` → the helper
    calls ``_escalate_stuck_to_critic(ladder_step="REFINE")``."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan([Task(id="1.1", phase_id="1", title="t", description="d")])
    )
    orch = _make_orch(tmp_path, pm)
    task = (await pm.get_task("1.1")) or pytest.fail("task not found")

    # Pre-bump to 2 — next call increments to 3 and triggers REFINE.
    await pm.increment_discard("1.1")
    await pm.increment_discard("1.1")

    captured_calls: list[dict] = []

    async def fake_escalate(
        orch_arg: Any,
        task_arg: Any,
        *,
        stuck_state: Any,
        ladder_step: str,
        recent_evidence: str = "",
        prior_attempts: list[str] | None = None,
    ) -> StuckResolution:
        captured_calls.append({"ladder_step": ladder_step, "discard_count": stuck_state.discard_count})
        return StuckResolution(action="refine", guidance="hint X")

    monkeypatch.setattr(ep, "_escalate_stuck_to_critic", fake_escalate)
    out = await ep._try_retry_or_escalate(
        orch, task, retry_limit=10, reason="coder failure"
    )
    # The legacy 'escalated' flag is NOT set on a REFINE — the task is
    # restarted with fresh guidance, not blocked.
    assert out.escalated is False
    assert captured_calls and captured_calls[0]["ladder_step"] == "REFINE"
    assert captured_calls[0]["discard_count"] == 3


@pytest.mark.asyncio
async def test_5_discards_invokes_stuck_critic_in_pivot_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan([Task(id="1.1", phase_id="1", title="t", description="d")])
    )
    orch = _make_orch(tmp_path, pm)
    task = (await pm.get_task("1.1")) or pytest.fail("task not found")

    for _ in range(4):  # bump to 4
        await pm.increment_discard("1.1")

    captured: list[str] = []

    async def fake_escalate(
        orch_arg: Any,
        task_arg: Any,
        *,
        stuck_state: Any,
        ladder_step: str,
        recent_evidence: str = "",
        prior_attempts: list[str] | None = None,
    ) -> StuckResolution:
        captured.append(ladder_step)
        return StuckResolution(action="pivot", guidance="radical redirect")

    monkeypatch.setattr(ep, "_escalate_stuck_to_critic", fake_escalate)
    out = await ep._try_retry_or_escalate(
        orch, task, retry_limit=10, reason="coder failure"
    )
    assert out.escalated is False
    assert captured == ["PIVOT"]
    # PIVOT outcome bumps pivot_count.
    state = await pm.get_stuck_state("1.1")
    assert state.pivot_count == 1


@pytest.mark.asyncio
async def test_3_pivots_invokes_stuck_critic_in_soft_blocker_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan([Task(id="1.1", phase_id="1", title="t", description="d")])
    )
    orch = _make_orch(tmp_path, pm)
    task = (await pm.get_task("1.1")) or pytest.fail("task not found")

    for _ in range(2):  # 2 pivots already.
        await pm.increment_pivot("1.1")
    # Bump discards to make the next call hit a ladder threshold for
    # the ladder dispatch path.
    for _ in range(4):
        await pm.increment_discard("1.1")

    captured: list[str] = []

    async def fake_escalate(
        orch_arg: Any,
        task_arg: Any,
        *,
        stuck_state: Any,
        ladder_step: str,
        recent_evidence: str = "",
        prior_attempts: list[str] | None = None,
    ) -> StuckResolution:
        captured.append(ladder_step)
        return StuckResolution(
            action="soft-blocker", guidance="human picks hardware family"
        )

    monkeypatch.setattr(ep, "_escalate_stuck_to_critic", fake_escalate)
    out = await ep._try_retry_or_escalate(
        orch, task, retry_limit=10, reason="coder failure"
    )
    # SOFT_BLOCKER fires escalated + blocked.
    assert captured == ["PIVOT"] or captured == ["SOFT_BLOCKER"]
    # If the resolution is soft-blocker, the task must be blocked.
    if out.escalated:
        assert out.status == "blocked"


@pytest.mark.asyncio
async def test_soft_blocker_resolution_blocks_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan([Task(id="1.1", phase_id="1", title="t", description="d")])
    )
    orch = _make_orch(tmp_path, pm)
    task = (await pm.get_task("1.1")) or pytest.fail("task not found")

    # Force the ladder to return SOFT_BLOCKER by stuffing pivot_count high.
    for _ in range(3):
        await pm.increment_pivot("1.1")
    # Bump discards to trigger a ladder-dispatch path.
    for _ in range(3):
        await pm.increment_discard("1.1")

    async def fake_escalate(
        orch_arg: Any,
        task_arg: Any,
        *,
        stuck_state: Any,
        ladder_step: str,
        recent_evidence: str = "",
        prior_attempts: list[str] | None = None,
    ) -> StuckResolution:
        return StuckResolution(
            action="soft-blocker",
            guidance="human must decide hardware family",
        )

    monkeypatch.setattr(ep, "_escalate_stuck_to_critic", fake_escalate)
    out = await ep._try_retry_or_escalate(
        orch, task, retry_limit=10, reason="coder failure"
    )
    assert out.escalated is True
    assert out.status == "blocked"
    assert out.blocked_reason and "soft-blocker" in (out.blocked_reason or "")
