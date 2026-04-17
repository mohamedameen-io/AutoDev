"""Tests for :mod:`src.orchestrator.plan_phase`."""

from __future__ import annotations

from pathlib import Path

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator.plan_phase import (
    PlanParseError,
    parse_plan_markdown,
)
from state.schemas import Plan

from stub_adapter import StubAdapter, ok


CANONICAL_PLAN_MD = """
# Plan: Add subtract(a, b)

## Phase 1: Implement

### Task 1.1: Add subtract function to math.py
  - Description: Add subtract(a, b) that returns a - b
  - Files: math.py
  - Acceptance:
    - [ ] Function subtract defined
    - [ ] Returns correct value for positive ints

### Task 1.2: Add pytest test
  - Description: Verify subtract with 3 positive cases
  - Files: test_math.py
  - Acceptance:
    - [ ] pytest passes

## Phase 2: Document

### Task 2.1: Update README
  - Description: mention subtract
  - Files: README.md
  - Acceptance:
    - [ ] README mentions subtract
"""


def test_parse_plan_markdown_canonical() -> None:
    plan = parse_plan_markdown(CANONICAL_PLAN_MD, spec_hash="deadbeef")
    assert isinstance(plan, Plan)
    assert plan.spec_hash == "deadbeef"
    assert plan.metadata["title"] == "Add subtract(a, b)"
    assert len(plan.phases) == 2
    p1, p2 = plan.phases
    assert p1.id == "1"
    assert p1.title == "Implement"
    assert len(p1.tasks) == 2
    t11 = p1.tasks[0]
    assert t11.id == "1.1"
    assert t11.title.startswith("Add subtract")
    assert t11.files == ["math.py"]
    assert len(t11.acceptance) == 2
    assert t11.acceptance[0].description.startswith("Function subtract")
    assert p2.id == "2"
    assert p2.tasks[0].id == "2.1"


def test_parse_plan_markdown_missing_title_raises() -> None:
    with pytest.raises(PlanParseError):
        parse_plan_markdown("## Phase 1: x\n### Task 1.1: y\n")


def test_parse_plan_markdown_missing_phases_raises() -> None:
    with pytest.raises(PlanParseError):
        parse_plan_markdown("# Plan: nothing\n")


def test_parse_plan_markdown_phase_without_tasks_raises() -> None:
    with pytest.raises(PlanParseError):
        parse_plan_markdown("# Plan: x\n## Phase 1: empty\n## Phase 2: also empty\n")


def test_parse_plan_markdown_without_description_uses_title() -> None:
    md = """
# Plan: minimal

## Phase 1: x

### Task 1.1: do the thing
"""
    plan = parse_plan_markdown(md)
    t = plan.phases[0].tasks[0]
    assert t.description == "do the thing"
    assert t.files == []
    assert t.acceptance == []


# --- Full-flow tests using StubAdapter -------------------------------------


def _make_orch(cwd: Path, adapter: StubAdapter) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    registry = build_registry(cfg)
    return Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-test-plan",
    )


@pytest.mark.asyncio
async def test_plan_phase_happy_path(tmp_path: Path) -> None:
    adapter = StubAdapter(
        {
            "explorer": ok("codebase has math.py and a pytest test"),
            "domain_expert": ok("no unusual domain considerations"),
            "architect": ok(CANONICAL_PLAN_MD),
        }
    )
    orch = _make_orch(tmp_path, adapter)
    plan = await orch.plan("Add subtract(a, b)")
    assert isinstance(plan, Plan)
    assert len(plan.phases) == 2
    assert adapter.count("explorer") == 1
    assert adapter.count("domain_expert") == 1
    assert adapter.count("architect") == 1
    evdir = tmp_path / ".autodev" / "evidence"
    assert (evdir / "plan-explore-explore.json").exists()
    assert (evdir / "plan-domain_expert-domain_expert.json").exists()


@pytest.mark.asyncio
async def test_plan_phase_parse_retry_on_bad_architect_output(
    tmp_path: Path,
) -> None:
    """First architect call returns malformed markdown; retry succeeds."""
    bad_then_good = [
        ok("NO HEADING AT ALL"),
        ok(CANONICAL_PLAN_MD),
    ]
    adapter = StubAdapter(
        {
            "explorer": ok("found stuff"),
            "domain_expert": ok("ok"),
            "architect": bad_then_good,
        }
    )
    orch = _make_orch(tmp_path, adapter)
    plan = await orch.plan("Add subtract")
    assert plan is not None
    assert adapter.count("architect") == 2


@pytest.mark.asyncio
async def test_plan_phase_persists_via_ledger(tmp_path: Path) -> None:
    adapter = StubAdapter(
        {
            "explorer": ok("ok"),
            "domain_expert": ok("ok"),
            "architect": ok(CANONICAL_PLAN_MD),
        }
    )
    orch = _make_orch(tmp_path, adapter)
    await orch.plan("Add subtract")
    from state.plan_manager import PlanManager

    pm = PlanManager(tmp_path, session_id="reader")
    plan = await pm.load()
    assert plan is not None
    assert len(plan.phases) == 2
    assert (tmp_path / ".autodev" / "plan.json").exists()
    assert (tmp_path / ".autodev" / "plan-ledger.jsonl").exists()
    assert (tmp_path / ".autodev" / "spec.md").exists()
    spec_text = (tmp_path / ".autodev" / "spec.md").read_text()
    assert "subtract" in spec_text
