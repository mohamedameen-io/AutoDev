"""E2E integration test: Go repo — init → plan → execute.

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
# Stub plan markdown
# ---------------------------------------------------------------------------

_GO_PLAN_MD = """
# Plan: Add Subtract function to Go module

## Phase 1: Implement Subtract

### Task 1.1: Add Subtract to math.go
  - Description: Add Subtract(a, b int) int returning a - b
  - Files: math.go
  - Acceptance:
    - [ ] Subtract function exported
    - [ ] returns correct value

### Task 1.2: Add Go test for Subtract
  - Description: Add a Go test covering Subtract
  - Files: math_test.go
  - Acceptance:
    - [ ] tests pass
"""


def _make_stub(extras: Iterable[tuple[str, object]] | None = None) -> StubAdapter:
    responses: dict[str, object] = {
        "explorer": ok("math.go has Add(); no Subtract yet"),
        "domain_expert": ok("Go integer arithmetic; no special considerations"),
        "architect": ok(_GO_PLAN_MD),
        "developer": AgentResult(
            success=True,
            text="added Subtract function",
            diff=(
                "diff --git a/math.go b/math.go\n"
                "+func Subtract(a, b int) int {\n"
                "+\treturn a - b\n"
                "+}\n"
            ),
            files_changed=[Path("math.go")],
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
async def test_go_init_produces_config(tmp_git_repo_go: Path) -> None:
    """autodev init writes .autodev/config.json for a Go repo."""
    cfg = make_autodev_config(tmp_git_repo_go)
    config_path = tmp_git_repo_go / ".autodev" / "config.json"
    assert config_path.exists()
    raw = json.loads(config_path.read_text())
    assert raw["schema_version"] == "1.0.0"
    assert "agents" in raw


@pytest.mark.integration
@pytest.mark.asyncio
async def test_go_plan_produces_plan_json(tmp_git_repo_go: Path) -> None:
    """plan phase writes .autodev/plan.json for a Go repo."""
    cfg = make_autodev_config(tmp_git_repo_go)
    adapter = _make_stub()
    registry = build_registry(cfg)

    orch = Orchestrator(
        cwd=tmp_git_repo_go,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="int-go-plan",
    )
    plan = await orch.plan("Add a Subtract(a, b int) function to the Go module")

    plan_path = tmp_git_repo_go / ".autodev" / "plan.json"
    assert plan_path.exists()

    assert len(plan.phases) >= 1
    assert len(plan.phases[0].tasks) >= 1
    assert plan.plan_id

    raw = json.loads(plan_path.read_text())
    assert "plan_id" in raw
    assert "phases" in raw


@pytest.mark.integration
@pytest.mark.asyncio
async def test_go_execute_produces_evidence_bundles(tmp_git_repo_go: Path) -> None:
    """execute phase produces evidence bundles for a Go repo."""
    cfg = make_autodev_config(tmp_git_repo_go)
    adapter = _make_stub()
    registry = build_registry(cfg)

    orch = Orchestrator(
        cwd=tmp_git_repo_go,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="int-go-plan",
    )
    plan = await orch.plan("Add a Subtract(a, b int) function to the Go module")
    assert len(plan.phases) >= 1

    orch2 = Orchestrator(
        cwd=tmp_git_repo_go,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="int-go-exec",
    )
    tasks = await orch2.execute()
    assert len(tasks) >= 1
    assert all(t.status == "complete" for t in tasks), (
        f"Expected all tasks complete, got: {[(t.id, t.status) for t in tasks]}"
    )

    evdir = tmp_git_repo_go / ".autodev" / "evidence"
    for task in tasks:
        assert (evdir / f"{task.id}-developer.json").exists()
        assert (evdir / f"{task.id}-review.json").exists()
        assert (evdir / f"{task.id}-test.json").exists()


@pytest.mark.integration
@pytest.mark.live
@pytest.mark.asyncio
async def test_go_live_plan(tmp_git_repo_go: Path, live_mode: bool) -> None:
    """Live test: real claude -p call for plan phase on a Go repo."""
    from adapters.claude_code import ClaudeCodeAdapter

    cfg = make_autodev_config(tmp_git_repo_go)
    adapter = ClaudeCodeAdapter()
    registry = build_registry(cfg)

    orch = Orchestrator(
        cwd=tmp_git_repo_go,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="int-go-live-plan",
    )
    plan = await orch.plan("Add a Subtract(a, b int) function to the Go module")
    assert len(plan.phases) >= 1
    assert (tmp_git_repo_go / ".autodev" / "plan.json").exists()
