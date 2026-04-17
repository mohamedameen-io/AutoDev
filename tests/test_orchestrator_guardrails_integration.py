"""Integration tests: orchestrator with tight guardrails raises cleanly."""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from adapters.types import AgentResult, ToolCall
from agents import build_registry
from config.defaults import default_config
from config.schema import GuardrailsConfig
from orchestrator import Orchestrator
from state.schemas import (
    AcceptanceCriterion,
    Phase,
    Plan,
    Task,
)

from stub_adapter import StubAdapter, ok


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan() -> Plan:
    return Plan(
        plan_id="p-gr",
        spec_hash="d",
        phases=[
            Phase(
                id="1",
                title="Work",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="Do something",
                        description="Implement it",
                        files=["foo.py"],
                        acceptance=[
                            AcceptanceCriterion(id="ac-1", description="tests pass"),
                        ],
                    )
                ],
            )
        ],
        created_at=_iso(),
        updated_at=_iso(),
    )


async def _make_orch(
    cwd: Path,
    adapter: StubAdapter,
    guardrails: GuardrailsConfig,
) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    cfg.guardrails = guardrails
    registry = build_registry(cfg)
    orch = Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-gr",
    )
    await orch.plan_manager.init_plan(_mk_plan())
    return orch


@pytest.mark.asyncio
async def test_tool_call_cap_blocks_task(tmp_path: Path) -> None:
    """Tight tool-call cap causes task to be marked blocked."""
    # Cap of 1 tool call total — coder returns 2 tool calls → cap exceeded.
    guardrails = GuardrailsConfig(
        max_tool_calls_per_task=1,
        max_duration_s_per_task=900,
        max_diff_bytes=5_242_880,
    )
    coder_result = AgentResult(
        success=True,
        text="wrote code",
        tool_calls=[ToolCall(tool="Read"), ToolCall(tool="Write")],
        duration_s=0.01,
    )
    adapter = StubAdapter({"developer": coder_result})
    orch = await _make_orch(tmp_path, adapter, guardrails)
    tasks = await orch.execute()
    assert len(tasks) == 1
    assert tasks[0].status == "blocked"


@pytest.mark.asyncio
async def test_diff_size_cap_blocks_task(tmp_path: Path) -> None:
    """Tight diff-size cap causes task to be marked blocked."""
    guardrails = GuardrailsConfig(
        max_tool_calls_per_task=60,
        max_duration_s_per_task=900,
        max_diff_bytes=5,  # 5 bytes — any real diff exceeds this
    )
    coder_result = AgentResult(
        success=True,
        text="wrote code",
        diff="diff --git a/foo.py b/foo.py\n+x = 1",
        duration_s=0.01,
    )
    adapter = StubAdapter({"developer": coder_result})
    orch = await _make_orch(tmp_path, adapter, guardrails)
    tasks = await orch.execute()
    assert len(tasks) == 1
    assert tasks[0].status == "blocked"


@pytest.mark.asyncio
async def test_invocation_cap_blocks_task(tmp_path: Path) -> None:
    """Tight invocation cap (max_tool_calls_per_task=1) blocks after 1 round-trip."""
    guardrails = GuardrailsConfig(
        max_tool_calls_per_task=1,
        max_duration_s_per_task=900,
        max_diff_bytes=5_242_880,
    )
    # Coder succeeds but reviewer triggers a second invocation (same role).
    # With cap=1, the second pre_invocation raises.
    coder_result = ok("wrote code")
    reviewer_result = ok("APPROVED\n- clean")
    adapter = StubAdapter({"developer": coder_result, "reviewer": reviewer_result})
    orch = await _make_orch(tmp_path, adapter, guardrails)
    tasks = await orch.execute()
    # Either coder or reviewer will hit the cap — task ends blocked.
    assert tasks[0].status == "blocked"


@pytest.mark.asyncio
async def test_guardrail_enforcer_attached_to_orchestrator(tmp_path: Path) -> None:
    """Orchestrator exposes .guardrails and .loop_detector attributes."""
    cfg = default_config()
    registry = build_registry(cfg)
    adapter = StubAdapter({})
    orch = Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
    )
    from guardrails import GuardrailEnforcer, LoopDetector
    assert isinstance(orch.guardrails, GuardrailEnforcer)
    assert isinstance(orch.loop_detector, LoopDetector)


@pytest.mark.asyncio
async def test_loop_detector_blocks_task_on_repeated_output(tmp_path: Path) -> None:
    """Repeated identical coder output triggers loop detection → task blocked."""
    guardrails = GuardrailsConfig(
        max_tool_calls_per_task=60,
        max_duration_s_per_task=900,
        max_diff_bytes=5_242_880,
    )
    # Coder always returns the same text; reviewer always needs changes.
    # After window=3 identical outputs, loop detector fires.
    repeated_coder = ok("identical output every time")
    reviewer_needs_changes = ok("NEEDS_CHANGES\n- fix it")
    adapter = StubAdapter(
        {
            "developer": repeated_coder,
            "reviewer": reviewer_needs_changes,
            "critic_sounding_board": ok("escalation"),
        }
    )
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    cfg.guardrails = guardrails
    cfg.qa_retry_limit = 10  # high retry limit so guardrail fires first
    registry = build_registry(cfg)
    orch = Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-loop",
    )
    await orch.plan_manager.init_plan(_mk_plan())
    tasks = await orch.execute()
    # Task should be blocked (either by loop detector or retry exhaustion).
    assert tasks[0].status == "blocked"
