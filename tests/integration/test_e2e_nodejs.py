"""E2E integration test: NodeJS/TypeScript repo — init → plan → execute.

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

_NODEJS_PLAN_MD = """
# Plan: Add farewell function to TypeScript module

## Phase 1: Implement farewell

### Task 1.1: Add farewell to src/index.ts
  - Description: Add farewell(name: string): string returning "Goodbye, name!"
  - Files: src/index.ts
  - Acceptance:
    - [ ] farewell function exported
    - [ ] returns correct string

### Task 1.2: Add Jest test for farewell
  - Description: Add a Jest test covering the farewell function
  - Files: src/index.test.ts
  - Acceptance:
    - [ ] tests pass
"""


def _make_stub(extras: Iterable[tuple[str, object]] | None = None) -> StubAdapter:
    responses: dict[str, object] = {
        "explorer": ok("src/index.ts exports greet(); no farewell yet"),
        "domain_expert": ok("TypeScript string interpolation; no special considerations"),
        "architect": ok(_NODEJS_PLAN_MD),
        "developer": AgentResult(
            success=True,
            text="added farewell function",
            diff=(
                "diff --git a/src/index.ts b/src/index.ts\n"
                "+export function farewell(name: string): string {\n"
                "+  return `Goodbye, ${name}!`;\n"
                "+}\n"
            ),
            files_changed=[Path("src/index.ts")],
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
async def test_nodejs_init_produces_config(tmp_git_repo_nodejs: Path) -> None:
    """autodev init writes .autodev/config.json for a NodeJS repo."""
    cfg = make_autodev_config(tmp_git_repo_nodejs)
    config_path = tmp_git_repo_nodejs / ".autodev" / "config.json"
    assert config_path.exists()
    raw = json.loads(config_path.read_text())
    assert raw["schema_version"] == "1.0.0"
    assert "agents" in raw


@pytest.mark.integration
@pytest.mark.asyncio
async def test_nodejs_plan_produces_plan_json(tmp_git_repo_nodejs: Path) -> None:
    """plan phase writes .autodev/plan.json for a NodeJS/TS repo."""
    cfg = make_autodev_config(tmp_git_repo_nodejs)
    adapter = _make_stub()
    registry = build_registry(cfg)

    orch = Orchestrator(
        cwd=tmp_git_repo_nodejs,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="int-node-plan",
    )
    plan = await orch.plan("Add a farewell(name) function to the TypeScript module")

    plan_path = tmp_git_repo_nodejs / ".autodev" / "plan.json"
    assert plan_path.exists()

    assert len(plan.phases) >= 1
    assert len(plan.phases[0].tasks) >= 1
    assert plan.plan_id

    raw = json.loads(plan_path.read_text())
    assert "plan_id" in raw
    assert "phases" in raw


@pytest.mark.integration
@pytest.mark.asyncio
async def test_nodejs_execute_produces_evidence_bundles(
    tmp_git_repo_nodejs: Path,
) -> None:
    """execute phase produces evidence bundles for a NodeJS repo."""
    cfg = make_autodev_config(tmp_git_repo_nodejs)
    adapter = _make_stub()
    registry = build_registry(cfg)

    orch = Orchestrator(
        cwd=tmp_git_repo_nodejs,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="int-node-plan",
    )
    plan = await orch.plan("Add a farewell(name) function to the TypeScript module")
    assert len(plan.phases) >= 1

    orch2 = Orchestrator(
        cwd=tmp_git_repo_nodejs,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="int-node-exec",
    )
    tasks = await orch2.execute()
    assert len(tasks) >= 1
    assert all(t.status == "complete" for t in tasks), (
        f"Expected all tasks complete, got: {[(t.id, t.status) for t in tasks]}"
    )

    evdir = tmp_git_repo_nodejs / ".autodev" / "evidence"
    for task in tasks:
        assert (evdir / f"{task.id}-developer.json").exists()
        assert (evdir / f"{task.id}-review.json").exists()
        assert (evdir / f"{task.id}-test.json").exists()


@pytest.mark.integration
@pytest.mark.live
@pytest.mark.asyncio
async def test_nodejs_live_plan(tmp_git_repo_nodejs: Path, live_mode: bool) -> None:
    """Live test: real claude -p call for plan phase on a NodeJS repo."""
    from adapters.claude_code import ClaudeCodeAdapter

    cfg = make_autodev_config(tmp_git_repo_nodejs)
    adapter = ClaudeCodeAdapter()
    registry = build_registry(cfg)

    orch = Orchestrator(
        cwd=tmp_git_repo_nodejs,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="int-node-live-plan",
    )
    plan = await orch.plan("Add a farewell(name) function to the TypeScript module")
    assert len(plan.phases) >= 1
    assert (tmp_git_repo_nodejs / ".autodev" / "plan.json").exists()
