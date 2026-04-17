"""Integration test: resume after simulated mid-execute crash.

Simulates a crash between tasks by running execute() for only the first task,
then verifying that ``orchestrator.resume()`` picks up from the ledger
checkpoint and completes the remaining tasks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pytest

from adapters.types import AgentResult
from agents import build_registry
from orchestrator import Orchestrator
from state.plan_manager import PlanManager

from stub_adapter import StubAdapter, ok
from helpers import make_autodev_config


# ---------------------------------------------------------------------------
# Stub plan markdown
# ---------------------------------------------------------------------------

_CRASH_TEST_PLAN_MD = """
# Plan: Add two functions

## Phase 1: Implement functions

### Task 1.1: Add multiply to math_utils.py
  - Description: Add multiply(a, b) returning a * b
  - Files: math_utils.py
  - Acceptance:
    - [ ] multiply function exists

### Task 1.2: Add divide to math_utils.py
  - Description: Add divide(a, b) returning a / b
  - Files: math_utils.py
  - Acceptance:
    - [ ] divide function exists
"""


def _make_stub(extras: Iterable[tuple[str, object]] | None = None) -> StubAdapter:
    responses: dict[str, object] = {
        "explorer": ok("math_utils.py has add(); no multiply or divide yet"),
        "domain_expert": ok("simple arithmetic; no special considerations"),
        "architect": ok(_CRASH_TEST_PLAN_MD),
        "developer": AgentResult(
            success=True,
            text="added function",
            diff="diff --git a/math_utils.py b/math_utils.py\n+def multiply(a,b): return a*b",
            files_changed=[Path("math_utils.py")],
            duration_s=0.01,
        ),
        "reviewer": ok("APPROVED\n- simple and correct"),
        "test_engineer": ok("RESULTS: passed=1 failed=0 total=1"),
    }
    if extras:
        responses.update(dict(extras))
    return StubAdapter(responses)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resume_picks_up_after_first_task(tmp_git_repo: Path) -> None:
    """Simulate crash after task 1.1; resume must complete task 1.2 only."""
    cfg = make_autodev_config(tmp_git_repo)
    registry = build_registry(cfg)

    # Step 1: Plan
    adapter = _make_stub()
    orch = Orchestrator(
        cwd=tmp_git_repo,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="crash-plan",
    )
    plan = await orch.plan("Add multiply and divide functions")
    assert len(plan.phases) >= 1
    task_ids = [t.id for t in plan.phases[0].tasks]
    assert len(task_ids) >= 2, "Need at least 2 tasks to test resume"

    first_task_id = task_ids[0]
    second_task_id = task_ids[1]

    # Step 2: Execute only the first task (simulates crash between tasks)
    adapter2 = _make_stub()
    orch2 = Orchestrator(
        cwd=tmp_git_repo,
        cfg=cfg,
        adapter=adapter2,
        registry=registry,
        session_id="crash-exec-partial",
    )
    partial_tasks = await orch2.execute(task_id=first_task_id)
    assert len(partial_tasks) == 1
    assert partial_tasks[0].id == first_task_id
    assert partial_tasks[0].status == "complete"

    # Step 3: Verify second task is still pending on disk
    pm = PlanManager(tmp_git_repo, session_id="crash-reader")
    mid_plan = await pm.load()
    assert mid_plan is not None

    task_statuses = {t.id: t.status for phase in mid_plan.phases for t in phase.tasks}
    assert task_statuses[first_task_id] == "complete", (
        f"Task {first_task_id} should be complete before resume"
    )
    assert task_statuses[second_task_id] == "pending", (
        f"Task {second_task_id} should still be pending before resume"
    )

    # Step 4: Resume — should pick up only the remaining task
    adapter3 = _make_stub()
    orch3 = Orchestrator(
        cwd=tmp_git_repo,
        cfg=cfg,
        adapter=adapter3,
        registry=registry,
        session_id="crash-resume",
    )
    resumed_tasks = await orch3.resume()
    assert len(resumed_tasks) >= 1
    resumed_ids = [t.id for t in resumed_tasks]
    assert second_task_id in resumed_ids, (
        f"Resume should have processed {second_task_id}, got: {resumed_ids}"
    )
    assert first_task_id not in resumed_ids, (
        f"Resume should NOT re-process {first_task_id}, got: {resumed_ids}"
    )

    # Step 5: All tasks complete after resume
    final_plan = await pm.load()
    assert final_plan is not None
    for phase in final_plan.phases:
        for task in phase.tasks:
            assert task.status == "complete", (
                f"Task {task.id} should be complete after resume, got: {task.status}"
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resume_with_no_plan_returns_empty(tmp_git_repo: Path) -> None:
    """resume() on a repo with no plan returns an empty list gracefully."""
    cfg = make_autodev_config(tmp_git_repo)
    registry = build_registry(cfg)
    adapter = _make_stub()

    orch = Orchestrator(
        cwd=tmp_git_repo,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="crash-no-plan",
    )
    result = await orch.resume()
    assert result == [], f"Expected empty list, got: {result}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resume_after_all_complete_is_noop(tmp_git_repo: Path) -> None:
    """resume() when all tasks are already complete returns an empty list."""
    cfg = make_autodev_config(tmp_git_repo)
    registry = build_registry(cfg)

    # Plan and fully execute
    adapter = _make_stub()
    orch = Orchestrator(
        cwd=tmp_git_repo,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="crash-full-plan",
    )
    await orch.plan("Add multiply and divide functions")

    orch2 = Orchestrator(
        cwd=tmp_git_repo,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="crash-full-exec",
    )
    tasks = await orch2.execute()
    assert all(t.status == "complete" for t in tasks)

    # Resume should be a no-op
    orch3 = Orchestrator(
        cwd=tmp_git_repo,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="crash-full-resume",
    )
    resumed = await orch3.resume()
    assert resumed == [], (
        f"Resume after full completion should return empty list, got: {resumed}"
    )
