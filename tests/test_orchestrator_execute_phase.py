"""Tests for :mod:`src.orchestrator.execute_phase`."""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from adapters.types import AgentResult
from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from state.plan_manager import PlanManager
from state.schemas import (
    AcceptanceCriterion,
    Phase,
    Plan,
    Task,
)

from stub_adapter import StubAdapter, ok, fail


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan(*, two_tasks: bool = False) -> Plan:
    tasks = [
        Task(
            id="1.1",
            phase_id="1",
            title="Add subtract",
            description="Implement subtract(a, b)",
            files=["math.py"],
            acceptance=[
                AcceptanceCriterion(id="ac-1", description="tests pass"),
            ],
        ),
    ]
    if two_tasks:
        tasks.append(
            Task(
                id="1.2",
                phase_id="1",
                title="Add divide",
                description="Implement divide(a, b)",
                files=["math.py"],
                acceptance=[
                    AcceptanceCriterion(id="ac-1", description="tests pass"),
                ],
            )
        )
    return Plan(
        plan_id="p-exec",
        spec_hash="d",
        phases=[Phase(id="1", title="Work", tasks=tasks)],
        created_at=_iso(),
        updated_at=_iso(),
    )


async def _make_orch_with_plan(
    cwd: Path, adapter: StubAdapter, *, two_tasks: bool = False
) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    # qa_retry_limit=3 by default is the retry-then-escalate threshold.
    registry = build_registry(cfg)
    orch = Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-exec",
    )
    await orch.plan_manager.init_plan(_mk_plan(two_tasks=two_tasks))
    return orch


def _coder_ok_with_diff() -> AgentResult:
    return AgentResult(
        success=True,
        text="wrote subtract",
        diff="diff --git a/math.py b/math.py\n+def subtract(a,b): return a-b",
        files_changed=[Path("math.py")],
        duration_s=0.1,
    )


def _reviewer(verdict: str) -> AgentResult:
    if verdict == "APPROVED":
        text = "APPROVED\n- clean"
    elif verdict == "NEEDS_CHANGES":
        text = "NEEDS_CHANGES\n- add a docstring"
    else:
        text = "REJECTED\n- completely wrong"
    return ok(text)


def _test_engineer_ok() -> AgentResult:
    return ok("ran pytest\nRESULTS: passed=3 failed=0 total=3")


def _test_engineer_fail() -> AgentResult:
    return ok("RESULTS: passed=0 failed=3 total=3\nAssertionError...")


@pytest.mark.asyncio
async def test_execute_happy_path(tmp_path: Path) -> None:
    adapter = StubAdapter(
        {
            "developer": _coder_ok_with_diff(),
            "reviewer": _reviewer("APPROVED"),
            "test_engineer": _test_engineer_ok(),
        }
    )
    orch = await _make_orch_with_plan(tmp_path, adapter)
    tasks = await orch.execute()
    assert len(tasks) == 1
    assert tasks[0].id == "1.1"
    assert tasks[0].status == "complete"
    # Evidence bundles written.
    evdir = tmp_path / ".autodev" / "evidence"
    assert (evdir / "1.1-developer.json").exists()
    assert (evdir / "1.1-review.json").exists()
    assert (evdir / "1.1-test.json").exists()
    assert (evdir / "1.1.patch").exists()


@pytest.mark.asyncio
async def test_execute_reviewer_needs_changes_retries(tmp_path: Path) -> None:
    adapter = StubAdapter(
        {
            "developer": [_coder_ok_with_diff(), _coder_ok_with_diff()],
            "reviewer": [_reviewer("NEEDS_CHANGES"), _reviewer("APPROVED")],
            "test_engineer": _test_engineer_ok(),
        }
    )
    orch = await _make_orch_with_plan(tmp_path, adapter)
    tasks = await orch.execute()
    assert tasks[0].status == "complete"
    assert tasks[0].retry_count == 1
    # Coder called twice (initial + retry), reviewer twice, test_engineer once.
    assert adapter.count("developer") == 2
    assert adapter.count("reviewer") == 2
    assert adapter.count("test_engineer") == 1


@pytest.mark.asyncio
async def test_execute_test_failure_retries_then_passes(tmp_path: Path) -> None:
    adapter = StubAdapter(
        {
            "developer": [_coder_ok_with_diff(), _coder_ok_with_diff()],
            "reviewer": _reviewer("APPROVED"),
            "test_engineer": [_test_engineer_fail(), _test_engineer_ok()],
        }
    )
    orch = await _make_orch_with_plan(tmp_path, adapter)
    tasks = await orch.execute()
    assert tasks[0].status == "complete"
    assert tasks[0].retry_count == 1


@pytest.mark.asyncio
async def test_execute_retry_exhaustion_escalates(tmp_path: Path) -> None:
    """After ``qa_retry_limit`` (default 3) failures, sounding_board is called."""
    adapter = StubAdapter(
        {
            "developer": _coder_ok_with_diff(),
            "reviewer": _reviewer("NEEDS_CHANGES"),  # always fails
            "test_engineer": _test_engineer_ok(),
            "critic_sounding_board": ok("escalation diagnosis: planning gap"),
        }
    )
    orch = await _make_orch_with_plan(tmp_path, adapter)
    tasks = await orch.execute()
    assert len(tasks) == 1
    task = tasks[0]
    assert task.escalated is True
    assert task.status == "blocked"
    assert adapter.count("critic_sounding_board") == 1
    # Ensure a critic evidence file was written.
    critic_ev = tmp_path / ".autodev" / "evidence" / "1.1-critic.json"
    assert critic_ev.exists()


@pytest.mark.asyncio
async def test_execute_coder_adapter_failure_retries_and_escalates(
    tmp_path: Path,
) -> None:
    adapter = StubAdapter(
        {
            "developer": fail("claude binary not found"),
            "reviewer": _reviewer("APPROVED"),
            "test_engineer": _test_engineer_ok(),
            "critic_sounding_board": ok("cannot run coder"),
        }
    )
    orch = await _make_orch_with_plan(tmp_path, adapter)
    tasks = await orch.execute()
    assert tasks[0].escalated is True
    assert tasks[0].status == "blocked"


@pytest.mark.asyncio
async def test_execute_multiple_tasks_sequence(tmp_path: Path) -> None:
    adapter = StubAdapter(
        {
            "developer": _coder_ok_with_diff(),
            "reviewer": _reviewer("APPROVED"),
            "test_engineer": _test_engineer_ok(),
        }
    )
    orch = await _make_orch_with_plan(tmp_path, adapter, two_tasks=True)
    tasks = await orch.execute()
    assert len(tasks) == 2
    assert all(t.status == "complete" for t in tasks)
    assert adapter.count("developer") == 2
    assert adapter.count("reviewer") == 2
    assert adapter.count("test_engineer") == 2


@pytest.mark.asyncio
async def test_execute_specific_task_id(tmp_path: Path) -> None:
    adapter = StubAdapter(
        {
            "developer": _coder_ok_with_diff(),
            "reviewer": _reviewer("APPROVED"),
            "test_engineer": _test_engineer_ok(),
        }
    )
    orch = await _make_orch_with_plan(tmp_path, adapter, two_tasks=True)
    tasks = await orch.execute(task_id="1.2")
    assert len(tasks) == 1
    assert tasks[0].id == "1.2"
    # Task 1.1 still pending.
    t11 = await orch.plan_manager.get_task("1.1")
    assert t11 is not None and t11.status == "pending"


@pytest.mark.asyncio
async def test_execute_unknown_task_id_raises(tmp_path: Path) -> None:
    from errors import AutodevError

    adapter = StubAdapter({})
    orch = await _make_orch_with_plan(tmp_path, adapter)
    with pytest.raises(AutodevError):
        await orch.execute(task_id="bogus")


@pytest.mark.asyncio
async def test_status_reports_counts(tmp_path: Path) -> None:
    adapter = StubAdapter(
        {
            "developer": _coder_ok_with_diff(),
            "reviewer": _reviewer("APPROVED"),
            "test_engineer": _test_engineer_ok(),
        }
    )
    orch = await _make_orch_with_plan(tmp_path, adapter, two_tasks=True)
    await orch.execute(task_id="1.1")
    snap = await orch.status()
    assert snap["plan"] is not None
    assert snap["totals"]["complete"] == 1
    assert snap["totals"]["pending"] == 1
    assert snap["totals"]["total"] == 2


@pytest.mark.asyncio
async def test_resume_picks_up_pending_tasks(tmp_path: Path) -> None:
    adapter = StubAdapter(
        {
            "developer": _coder_ok_with_diff(),
            "reviewer": _reviewer("APPROVED"),
            "test_engineer": _test_engineer_ok(),
        }
    )
    orch = await _make_orch_with_plan(tmp_path, adapter, two_tasks=True)
    await orch.execute(task_id="1.1")  # finish task 1 only
    # Fresh orchestrator / adapter for resume.
    adapter2 = StubAdapter(
        {
            "developer": _coder_ok_with_diff(),
            "reviewer": _reviewer("APPROVED"),
            "test_engineer": _test_engineer_ok(),
        }
    )
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    orch2 = Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=adapter2,
        registry=build_registry(cfg),
        session_id="sess-resume",
    )
    tasks = await orch2.resume()
    assert [t.id for t in tasks] == ["1.2"]
    pm = PlanManager(tmp_path, session_id="reader")
    final = await pm.load()
    assert final is not None
    assert all(t.status == "complete" for p in final.phases for t in p.tasks)
