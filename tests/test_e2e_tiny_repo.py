"""End-to-end test on a tiny git repo using :class:`StubAdapter`.

Mirrors the ``autodev init → plan → execute → status`` CLI flow using
in-process calls. We can't easily inject a stub adapter through
``click.CliRunner`` because the CLI commands resolve an adapter via
``get_adapter(cfg.platform)``, which does a live healthcheck. This test
exercises the identical code paths at the orchestrator layer and spot-
checks the CLI surface (help, status) via ``CliRunner``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterable

import pytest
from click.testing import CliRunner

from adapters.types import AgentResult
from agents import build_registry
from cli import cli
from config.defaults import default_config
from config.loader import save_config
from config.schema import AutodevConfig
from orchestrator import Orchestrator
from state.plan_manager import PlanManager

from stub_adapter import StubAdapter, ok


PLAN_MD = """
# Plan: Add subtract(a, b) to math module

## Phase 1: Implement and test

### Task 1.1: Add subtract to math.py
  - Description: Add subtract(a, b) returning a - b
  - Files: math.py
  - Acceptance:
    - [ ] subtract function exists
    - [ ] returns correct value

### Task 1.2: Add pytest test for subtract
  - Description: Add a pytest covering positive and negative cases
  - Files: test_math.py
  - Acceptance:
    - [ ] tests pass
"""


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    (repo / "math.py").write_text("def add(a, b):\n    return a + b\n")
    subprocess.run(
        ["git", "config", "user.email", "t@t"],
        cwd=str(repo),
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "t"],
        cwd=str(repo),
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "commit", "-qm", "initial"], cwd=str(repo), check=True
    )


def _init_autodev(repo: Path) -> AutodevConfig:
    cfg = default_config()
    cfg.platform = "claude_code"  # any concrete value; stub adapter ignores it
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    save_config(cfg, repo / ".autodev" / "config.json")
    return cfg


def _coder_result() -> AgentResult:
    return AgentResult(
        success=True,
        text="added subtract",
        diff="diff --git a/math.py b/math.py\n+def subtract(a,b): return a-b",
        files_changed=[Path("math.py")],
        duration_s=0.01,
    )


def _stub(extras: Iterable[tuple[str, object]] | None = None) -> StubAdapter:
    """Default stub responses for the full plan+execute flow."""
    responses: dict[str, object] = {
        "explorer": ok("math.py has add(); no tests yet"),
        "domain_expert": ok("simple arithmetic; no special considerations"),
        "architect": ok(PLAN_MD),
        "developer": _coder_result(),
        "reviewer": ok("APPROVED\n- simple and correct"),
        "test_engineer": ok("RESULTS: passed=2 failed=0 total=2"),
    }
    if extras:
        responses.update(dict(extras))
    return StubAdapter(responses)


@pytest.mark.asyncio
async def test_e2e_plan_execute_status(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    cfg = _init_autodev(repo)
    adapter = _stub()
    registry = build_registry(cfg)

    # 1. plan
    orch = Orchestrator(
        cwd=repo,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="e2e-1",
    )
    plan = await orch.plan("Add a subtract(a, b) function with a pytest test")
    assert (repo / ".autodev" / "plan.json").exists()
    assert len(plan.phases) == 1
    assert len(plan.phases[0].tasks) == 2

    # 2. execute
    orch2 = Orchestrator(
        cwd=repo,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="e2e-2",
    )
    tasks = await orch2.execute()
    assert len(tasks) == 2
    assert all(t.status == "complete" for t in tasks)

    # 3. status
    orch3 = Orchestrator(
        cwd=repo,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="e2e-3",
    )
    snap = await orch3.status()
    assert snap["totals"]["complete"] == 2
    assert snap["totals"]["pending"] == 0

    # Evidence bundles exist for both tasks.
    evdir = repo / ".autodev" / "evidence"
    for tid in ("1.1", "1.2"):
        assert (evdir / f"{tid}-developer.json").exists()
        assert (evdir / f"{tid}-review.json").exists()
        assert (evdir / f"{tid}-test.json").exists()
        assert (evdir / f"{tid}.patch").exists()


@pytest.mark.asyncio
async def test_e2e_kill_and_resume(tmp_path: Path) -> None:
    """Simulate mid-execute interruption: run exactly one task, then resume."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    cfg = _init_autodev(repo)
    registry = build_registry(cfg)

    # Plan once.
    adapter = _stub()
    orch = Orchestrator(
        cwd=repo,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="e2e-1",
    )
    await orch.plan("Add subtract")

    # Execute only task 1.1 (simulates kill between tasks).
    adapter2 = _stub()
    orch2 = Orchestrator(
        cwd=repo,
        cfg=cfg,
        adapter=adapter2,
        registry=registry,
        session_id="e2e-2",
    )
    tasks = await orch2.execute(task_id="1.1")
    assert tasks[0].status == "complete"

    # Verify 1.2 still pending on disk before resume.
    pm = PlanManager(repo, session_id="reader")
    mid = await pm.load()
    assert mid is not None
    t11, t12 = mid.phases[0].tasks[0], mid.phases[0].tasks[1]
    assert t11.status == "complete"
    assert t12.status == "pending"

    # Resume with a fresh orchestrator — should pick up 1.2 only.
    adapter3 = _stub()
    orch3 = Orchestrator(
        cwd=repo,
        cfg=cfg,
        adapter=adapter3,
        registry=registry,
        session_id="e2e-3",
    )
    resumed = await orch3.resume()
    assert [t.id for t in resumed] == ["1.2"]

    # All tasks complete now.
    final = await pm.load()
    assert final is not None
    assert all(t.status == "complete" for t in final.phases[0].tasks)


def test_cli_status_outside_project_exits_cleanly(tmp_path: Path) -> None:
    """``autodev status`` in a non-project dir gives a pointer, not a crash."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(cli, ["status"])
    assert result.exit_code == 1
    assert "autodev init" in result.output
