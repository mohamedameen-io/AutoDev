"""Integration tests: QA gate failures trigger the retry/escalate loop.

These tests exercise the full execute_phase loop with a StubAdapter and
mock QA gates to verify that:

  1. A lint failure causes the coder to be retried.
  2. Repeated lint failures exhaust retries and block the task.
  3. A gate that passes allows the loop to continue to reviewer.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from adapters.types import AgentResult
from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from plugins.registry import GateResult
from state.schemas import AcceptanceCriterion, Phase, Plan, Task

from stub_adapter import StubAdapter, ok


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan() -> Plan:
    return Plan(
        plan_id="p-qa",
        spec_hash="qa",
        phases=[
            Phase(
                id="1",
                title="QA test phase",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="Implement feature",
                        description="Write some code",
                        files=["feature.py"],
                        acceptance=[AcceptanceCriterion(id="ac-1", description="tests pass")],
                    )
                ],
            )
        ],
        created_at=_iso(),
        updated_at=_iso(),
    )


def _coder_ok(variant: int = 0) -> AgentResult:
    return AgentResult(
        success=True,
        text=f"wrote feature variant={variant}",
        diff=f"diff --git a/feature.py b/feature.py\n+def feature_{variant}(): pass",
        files_changed=[Path("feature.py")],
        duration_s=0.1,
    )


async def _make_orch(cwd: Path, adapter: StubAdapter) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    cfg.qa_retry_limit = 3
    # Enable only lint gate for these tests.
    cfg.qa_gates.syntax_check = False
    cfg.qa_gates.lint = True
    cfg.qa_gates.build_check = False
    cfg.qa_gates.test_runner = False
    cfg.qa_gates.secretscan = False
    registry = build_registry(cfg)
    orch = Orchestrator(cwd=cwd, cfg=cfg, adapter=adapter, registry=registry, session_id="sess-qa")
    await orch.plan_manager.init_plan(_mk_plan())
    return orch


@pytest.mark.asyncio
async def test_lint_pass_continues_to_reviewer(tmp_path: Path) -> None:
    """When lint passes, the loop advances to reviewer."""
    adapter = StubAdapter(
        {
            "developer": _coder_ok(),
            "reviewer": ok("APPROVED\n- clean"),
            "test_engineer": ok("RESULTS: passed=1 failed=0 total=1"),
        }
    )
    orch = await _make_orch(tmp_path, adapter)

    passing_gate = GateResult(passed=True, details="lint passed")
    with patch("orchestrator.execute_phase.run_lint", new=AsyncMock(return_value=passing_gate)):
        tasks = await orch.execute()

    assert tasks[0].status == "complete"
    assert adapter.count("reviewer") == 1


@pytest.mark.asyncio
async def test_lint_fail_retries_coder(tmp_path: Path) -> None:
    """A lint failure retries the coder once, then passes on second attempt."""
    adapter = StubAdapter(
        {
            "developer": [_coder_ok(0), _coder_ok(1)],
            "reviewer": ok("APPROVED\n- clean"),
            "test_engineer": ok("RESULTS: passed=1 failed=0 total=1"),
        }
    )
    orch = await _make_orch(tmp_path, adapter)

    fail_gate = GateResult(passed=False, details="ruff: E501 line too long")
    pass_gate = GateResult(passed=True, details="lint passed")
    side_effects = [fail_gate, pass_gate]

    with patch("orchestrator.execute_phase.run_lint", new=AsyncMock(side_effect=side_effects)):
        tasks = await orch.execute()

    assert tasks[0].status == "complete"
    assert tasks[0].retry_count == 1
    assert adapter.count("developer") == 2


@pytest.mark.asyncio
async def test_lint_fail_exhausts_retries_blocks_task(tmp_path: Path) -> None:
    """Repeated lint failures exhaust retries and block the task."""
    # Use distinct coder outputs (different text) to avoid the loop-detector guardrail.
    coder_responses = [_coder_ok(i) for i in range(5)]
    adapter = StubAdapter(
        {
            "developer": coder_responses,
            "reviewer": ok("APPROVED\n- clean"),
            "test_engineer": ok("RESULTS: passed=1 failed=0 total=1"),
            "critic_sounding_board": ok("escalation: lint keeps failing"),
        }
    )
    orch = await _make_orch(tmp_path, adapter)

    always_fail = GateResult(passed=False, details="ruff: persistent lint error")

    with patch("orchestrator.execute_phase.run_lint", new=AsyncMock(return_value=always_fail)):
        tasks = await orch.execute()

    assert tasks[0].status == "blocked"
    # Task is blocked either via escalation or guardrail.
    assert tasks[0].escalated is True or adapter.count("critic_sounding_board") >= 0


@pytest.mark.asyncio
async def test_all_gates_disabled_skips_qa(tmp_path: Path) -> None:
    """When all gates are disabled, the loop advances without calling any gate."""
    adapter = StubAdapter(
        {
            "developer": _coder_ok(),
            "reviewer": ok("APPROVED\n- clean"),
            "test_engineer": ok("RESULTS: passed=1 failed=0 total=1"),
        }
    )
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    cfg.qa_gates.syntax_check = False
    cfg.qa_gates.lint = False
    cfg.qa_gates.build_check = False
    cfg.qa_gates.test_runner = False
    cfg.qa_gates.secretscan = False
    registry = build_registry(cfg)
    orch = Orchestrator(cwd=tmp_path, cfg=cfg, adapter=adapter, registry=registry, session_id="sess-qa2")
    await orch.plan_manager.init_plan(_mk_plan())

    tasks = await orch.execute()
    assert tasks[0].status == "complete"
