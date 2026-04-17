"""E2E integration test: Python repo — init → plan → execute.

Uses StubAdapter by default (no live LLM calls).
Set AUTODEV_LIVE=1 to run against real ``claude -p``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

import pytest

from adapters.types import AgentResult
from agents import build_registry
from orchestrator import Orchestrator

from stub_adapter import StubAdapter, ok
from helpers import make_autodev_config


# ---------------------------------------------------------------------------
# Stub plan markdown — deterministic output from the architect role
# ---------------------------------------------------------------------------

_PYTHON_PLAN_MD = """
# Plan: Add subtract function

## Phase 1: Implement subtract

### Task 1.1: Add subtract to math_utils.py
  - Description: Add subtract(a, b) returning a - b
  - Files: math_utils.py
  - Acceptance:
    - [ ] subtract function exists
    - [ ] returns correct value

### Task 1.2: Add pytest test for subtract
  - Description: Add a pytest covering positive and negative cases
  - Files: test_math_utils.py
  - Acceptance:
    - [ ] tests pass
"""


def _make_stub(extras: Iterable[tuple[str, object]] | None = None) -> StubAdapter:
    responses: dict[str, object] = {
        "explorer": ok("math_utils.py has add(); no tests for subtract yet"),
        "domain_expert": ok("simple arithmetic; no special considerations"),
        "architect": ok(_PYTHON_PLAN_MD),
        "developer": AgentResult(
            success=True,
            text="added subtract function",
            diff="diff --git a/math_utils.py b/math_utils.py\n+def subtract(a,b): return a-b",
            files_changed=[Path("math_utils.py")],
            duration_s=0.01,
        ),
        "reviewer": ok("APPROVED\n- simple and correct"),
        "test_engineer": ok("RESULTS: passed=2 failed=0 total=2"),
    }
    if extras:
        responses.update(dict(extras))
    return StubAdapter(responses)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_python_init_produces_config(tmp_git_repo: Path) -> None:
    """autodev init writes .autodev/config.json."""
    cfg = make_autodev_config(tmp_git_repo)
    config_path = tmp_git_repo / ".autodev" / "config.json"
    assert config_path.exists(), "config.json must exist after init"
    raw = json.loads(config_path.read_text())
    assert raw["schema_version"] == "1.0.0"
    assert "agents" in raw


@pytest.mark.integration
@pytest.mark.asyncio
async def test_python_plan_produces_plan_json(tmp_git_repo: Path) -> None:
    """plan phase writes .autodev/plan.json with expected structure."""
    cfg = make_autodev_config(tmp_git_repo)
    adapter = _make_stub()
    registry = build_registry(cfg)

    orch = Orchestrator(
        cwd=tmp_git_repo,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="int-py-plan",
    )
    plan = await orch.plan("Add a subtract(a, b) function with a pytest test")

    plan_path = tmp_git_repo / ".autodev" / "plan.json"
    assert plan_path.exists(), "plan.json must exist after plan phase"

    # Structural snapshot — not exact text
    assert len(plan.phases) >= 1
    assert len(plan.phases[0].tasks) >= 1
    assert plan.plan_id
    assert plan.spec_hash

    # Verify plan.json is valid JSON with required keys
    raw = json.loads(plan_path.read_text())
    assert "plan_id" in raw
    assert "phases" in raw
    assert isinstance(raw["phases"], list)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_python_execute_produces_evidence_bundles(tmp_git_repo: Path) -> None:
    """execute phase produces coder/review/test evidence bundles for each task."""
    cfg = make_autodev_config(tmp_git_repo)
    adapter = _make_stub()
    registry = build_registry(cfg)

    # Plan first
    orch = Orchestrator(
        cwd=tmp_git_repo,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="int-py-plan",
    )
    plan = await orch.plan("Add a subtract(a, b) function with a pytest test")
    assert len(plan.phases) >= 1

    # Execute
    orch2 = Orchestrator(
        cwd=tmp_git_repo,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="int-py-exec",
    )
    tasks = await orch2.execute()
    assert len(tasks) >= 1
    assert all(t.status == "complete" for t in tasks), (
        f"Expected all tasks complete, got: {[(t.id, t.status) for t in tasks]}"
    )

    # Evidence bundles exist
    evdir = tmp_git_repo / ".autodev" / "evidence"
    for task in tasks:
        assert (evdir / f"{task.id}-developer.json").exists(), (
            f"Missing coder evidence for task {task.id}"
        )
        assert (evdir / f"{task.id}-review.json").exists(), (
            f"Missing review evidence for task {task.id}"
        )
        assert (evdir / f"{task.id}-test.json").exists(), (
            f"Missing test evidence for task {task.id}"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_python_status_after_execute(tmp_git_repo: Path) -> None:
    """status() returns correct totals after full execute."""
    cfg = make_autodev_config(tmp_git_repo)
    adapter = _make_stub()
    registry = build_registry(cfg)

    orch = Orchestrator(
        cwd=tmp_git_repo,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="int-py-status-plan",
    )
    await orch.plan("Add a subtract(a, b) function with a pytest test")

    orch2 = Orchestrator(
        cwd=tmp_git_repo,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="int-py-status-exec",
    )
    tasks = await orch2.execute()
    total = len(tasks)

    orch3 = Orchestrator(
        cwd=tmp_git_repo,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="int-py-status-check",
    )
    snap = await orch3.status()
    assert snap["totals"]["complete"] == total
    assert snap["totals"]["pending"] == 0
    assert snap["plan"] is not None


@pytest.mark.integration
@pytest.mark.live
@pytest.mark.asyncio
async def test_python_live_plan(tmp_git_repo: Path, live_mode: bool) -> None:
    """Live test: real claude -p call for plan phase."""
    from adapters.claude_code import ClaudeCodeAdapter

    cfg = make_autodev_config(tmp_git_repo)
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    adapter = ClaudeCodeAdapter()
    registry = build_registry(cfg)

    orch = Orchestrator(
        cwd=tmp_git_repo,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="int-py-live-plan",
    )
    plan = await orch.plan("Add a subtract(a, b) function with a pytest test")
    assert len(plan.phases) >= 1
    assert (tmp_git_repo / ".autodev" / "plan.json").exists()
