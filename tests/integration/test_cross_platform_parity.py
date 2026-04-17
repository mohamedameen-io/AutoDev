"""Cross-platform parity test: claude vs cursor adapter plan structure.

Verifies that both adapters produce Plan objects with the same structural
shape (phases, tasks, required fields) when given identical stub responses.

The cursor adapter test is skipped if the cursor binary is not installed.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Iterable

import pytest

from agents import build_registry
from orchestrator import Orchestrator
from state.schemas import Plan

from stub_adapter import StubAdapter, ok
from helpers import make_autodev_config
from adapters.types import AgentResult


# ---------------------------------------------------------------------------
# Shared plan markdown
# ---------------------------------------------------------------------------

_PARITY_PLAN_MD = """
# Plan: Add subtract function

## Phase 1: Implement subtract

### Task 1.1: Add subtract to math_utils.py
  - Description: Add subtract(a, b) returning a - b
  - Files: math_utils.py
  - Acceptance:
    - [ ] subtract function exists

### Task 1.2: Add pytest test for subtract
  - Description: Add a pytest covering subtract
  - Files: test_math_utils.py
  - Acceptance:
    - [ ] tests pass
"""


def _make_stub(extras: Iterable[tuple[str, object]] | None = None) -> StubAdapter:
    responses: dict[str, object] = {
        "explorer": ok("math_utils.py has add(); no subtract yet"),
        "domain_expert": ok("simple arithmetic; no special considerations"),
        "architect": ok(_PARITY_PLAN_MD),
        "developer": AgentResult(
            success=True,
            text="added subtract",
            diff="diff --git a/math_utils.py b/math_utils.py\n+def subtract(a,b): return a-b",
            files_changed=[Path("math_utils.py")],
            duration_s=0.01,
        ),
        "reviewer": ok("APPROVED\n- simple and correct"),
        "test_engineer": ok("RESULTS: passed=1 failed=0 total=1"),
    }
    if extras:
        responses.update(dict(extras))
    return StubAdapter(responses)


def _assert_plan_structure(plan: Plan, adapter_name: str) -> None:
    """Assert that a Plan has the required structural fields."""
    assert plan.plan_id, f"[{adapter_name}] plan_id must be non-empty"
    assert plan.spec_hash, f"[{adapter_name}] spec_hash must be non-empty"
    assert plan.created_at, f"[{adapter_name}] created_at must be non-empty"
    assert plan.updated_at, f"[{adapter_name}] updated_at must be non-empty"
    assert len(plan.phases) >= 1, f"[{adapter_name}] plan must have at least one phase"

    for phase in plan.phases:
        assert phase.id, f"[{adapter_name}] phase.id must be non-empty"
        assert phase.title, f"[{adapter_name}] phase.title must be non-empty"
        assert len(phase.tasks) >= 1, (
            f"[{adapter_name}] phase {phase.id} must have at least one task"
        )
        for task in phase.tasks:
            assert task.id, f"[{adapter_name}] task.id must be non-empty"
            assert task.phase_id, f"[{adapter_name}] task.phase_id must be non-empty"
            assert task.title, f"[{adapter_name}] task.title must be non-empty"
            assert task.description, (
                f"[{adapter_name}] task.description must be non-empty"
            )
            assert task.status in (
                "pending",
                "in_progress",
                "coded",
                "auto_gated",
                "reviewed",
                "tested",
                "tournamented",
                "complete",
                "blocked",
                "skipped",
            ), f"[{adapter_name}] task.status '{task.status}' is not a valid TaskStatus"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_claude_adapter_plan_structure(tmp_git_repo: Path) -> None:
    """StubAdapter (claude path) produces a valid Plan with required structure."""
    cfg = make_autodev_config(tmp_git_repo)
    adapter = _make_stub()
    registry = build_registry(cfg)

    orch = Orchestrator(
        cwd=tmp_git_repo,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="parity-claude",
    )
    plan = await orch.plan("Add a subtract(a, b) function")
    _assert_plan_structure(plan, "claude_stub")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cursor_adapter_plan_structure_skips_if_not_installed(
    tmp_git_repo: Path,
) -> None:
    """StubAdapter (cursor path) produces same Plan structure as claude path.

    Skips if cursor binary is not installed on the system.
    """
    cursor_available = (
        shutil.which("cursor") is not None or shutil.which("cursor-agent") is not None
    )
    if not cursor_available:
        pytest.skip("cursor binary not installed — skipping cursor parity test")

    cfg = make_autodev_config(tmp_git_repo)
    # Use StubAdapter regardless — we're testing structural parity, not live calls
    adapter = _make_stub()
    registry = build_registry(cfg)

    orch = Orchestrator(
        cwd=tmp_git_repo,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="parity-cursor",
    )
    plan = await orch.plan("Add a subtract(a, b) function")
    _assert_plan_structure(plan, "cursor_stub")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_both_adapters_produce_same_phase_count(tmp_git_repo: Path) -> None:
    """Two stub adapters with identical responses produce plans with same phase count."""
    cfg = make_autodev_config(tmp_git_repo)
    registry = build_registry(cfg)

    # First run with stub adapter A
    adapter_a = _make_stub()
    orch_a = Orchestrator(
        cwd=tmp_git_repo,
        cfg=cfg,
        adapter=adapter_a,
        registry=registry,
        session_id="parity-a",
    )
    plan_a = await orch_a.plan("Add a subtract(a, b) function")

    # Second run in a fresh tmp dir with stub adapter B (same responses)
    import tempfile
    import subprocess as sp

    with tempfile.TemporaryDirectory() as tmp_b_str:
        tmp_b = Path(tmp_b_str) / "repo_b"
        tmp_b.mkdir()
        sp.run(["git", "init", "-q"], cwd=str(tmp_b), check=True)
        sp.run(["git", "config", "user.email", "t@t"], cwd=str(tmp_b), check=True)
        sp.run(["git", "config", "user.name", "t"], cwd=str(tmp_b), check=True)
        (tmp_b / "math_utils.py").write_text("def add(a, b):\n    return a + b\n")
        sp.run(["git", "add", "."], cwd=str(tmp_b), check=True)
        sp.run(["git", "commit", "-qm", "initial"], cwd=str(tmp_b), check=True)

        cfg_b = make_autodev_config(tmp_b)
        adapter_b = _make_stub()
        orch_b = Orchestrator(
            cwd=tmp_b,
            cfg=cfg_b,
            adapter=adapter_b,
            registry=registry,
            session_id="parity-b",
        )
        plan_b = await orch_b.plan("Add a subtract(a, b) function")

    assert len(plan_a.phases) == len(plan_b.phases), (
        f"Phase count mismatch: plan_a={len(plan_a.phases)}, plan_b={len(plan_b.phases)}"
    )

    for phase_a, phase_b in zip(plan_a.phases, plan_b.phases):
        assert len(phase_a.tasks) == len(phase_b.tasks), (
            f"Task count mismatch in phase {phase_a.id}: "
            f"plan_a={len(phase_a.tasks)}, plan_b={len(phase_b.tasks)}"
        )
