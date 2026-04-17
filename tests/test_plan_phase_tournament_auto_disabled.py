"""Tests for the plan-tournament auto-disable path.

When ``cfg.tournaments.auto_disable_for_models`` matches the resolved
tournament model (e.g. the judge is an opus-tier model), the plan phase
must skip the tournament entirely — no tournament subprocess calls, no
ledger entry, initial plan markdown flows straight to init_plan.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator.plan_tournament_runner import (
    _is_auto_disabled,
    _resolve_tournament_model,
    run_plan_tournament,
)

from stub_adapter import StubAdapter, ok


CANONICAL_PLAN_MD = """
# Plan: Add noop()

## Phase 1: Implement

### Task 1.1: Add noop function
  - Description: add noop that returns None
  - Files: noop.py
  - Acceptance:
    - [ ] function exists
"""


def _orch_with_judge_model(
    cwd: Path,
    adapter: StubAdapter,
    *,
    judge_model: str,
    auto_disable: list[str] | None = None,
) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = True
    cfg.tournaments.plan.num_judges = 1
    cfg.tournaments.plan.convergence_k = 1
    cfg.tournaments.plan.max_rounds = 3
    if auto_disable is not None:
        cfg.tournaments.auto_disable_for_models = auto_disable
    cfg.tournaments.impl.enabled = False
    cfg.agents["judge"].model = judge_model
    registry = build_registry(cfg)
    return Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-test-autodisable",
    )


# ── Pure-function tests for the auto-disable predicates ──────────────────────


def test_is_auto_disabled_exact_match() -> None:
    assert _is_auto_disabled("opus", ["opus"]) is True


def test_is_auto_disabled_substring_match() -> None:
    """Match is case-insensitive substring — matches real model IDs."""
    assert _is_auto_disabled("claude-opus-4-20250514", ["opus"]) is True
    assert _is_auto_disabled("CLAUDE-OPUS", ["opus"]) is True


def test_is_auto_disabled_no_match() -> None:
    assert _is_auto_disabled("sonnet", ["opus"]) is False
    assert _is_auto_disabled("haiku", ["opus"]) is False


def test_is_auto_disabled_none_or_empty() -> None:
    assert _is_auto_disabled(None, ["opus"]) is False
    assert _is_auto_disabled("opus", []) is False
    assert _is_auto_disabled("", ["opus"]) is False


def test_resolve_tournament_model_uses_judge(tmp_path: Path) -> None:
    adapter = StubAdapter({})
    orch = _orch_with_judge_model(tmp_path, adapter, judge_model="sonnet")
    assert _resolve_tournament_model(orch) == "sonnet"


def test_resolve_tournament_model_opus_tier(tmp_path: Path) -> None:
    adapter = StubAdapter({})
    orch = _orch_with_judge_model(tmp_path, adapter, judge_model="opus")
    assert _resolve_tournament_model(orch) == "opus"


# ── End-to-end test: run_plan_tournament is a no-op when auto-disabled ──


@pytest.mark.asyncio
async def test_run_plan_tournament_returns_initial_when_auto_disabled(
    tmp_path: Path,
) -> None:
    """``run_plan_tournament`` short-circuits: no tournament calls, no ledger entry."""
    adapter = StubAdapter(
        {
            "explorer": ok("ok"),
            "domain_expert": ok("ok"),
            "architect": ok(CANONICAL_PLAN_MD),
        }
    )
    orch = _orch_with_judge_model(
        tmp_path, adapter, judge_model="opus", auto_disable=["opus"]
    )

    result = await run_plan_tournament(orch, CANONICAL_PLAN_MD, "add noop()")
    assert result == CANONICAL_PLAN_MD

    assert adapter.count("critic_t") == 0
    assert adapter.count("architect_b") == 0
    assert adapter.count("synthesizer") == 0
    assert adapter.count("judge") == 0

    ledger = await orch.plan_manager.read_ledger()
    assert all(e.op != "plan_tournament_complete" for e in ledger)


# ── Full plan phase: auto-disable means initial plan goes directly to init_plan ──


@pytest.mark.asyncio
async def test_plan_phase_skips_tournament_when_judge_is_opus(
    tmp_path: Path,
) -> None:
    """Full plan phase with opus judge: tournament skipped, plan approved directly."""
    adapter = StubAdapter(
        {
            "explorer": ok("ok"),
            "domain_expert": ok("ok"),
            "architect": ok(CANONICAL_PLAN_MD),
        }
    )
    orch = _orch_with_judge_model(
        tmp_path, adapter, judge_model="opus", auto_disable=["opus"]
    )

    plan = await orch.plan("Add noop()")
    assert plan is not None

    assert adapter.count("critic_t") == 0
    assert adapter.count("architect_b") == 0
    assert adapter.count("synthesizer") == 0
    assert adapter.count("judge") == 0

    tournaments_root = tmp_path / ".autodev" / "tournaments"
    if tournaments_root.exists():
        assert not any(
            d.is_dir() and d.name.startswith("plan-")
            for d in tournaments_root.iterdir()
        )

    ledger = await orch.plan_manager.read_ledger()
    assert all(e.op != "plan_tournament_complete" for e in ledger)


@pytest.mark.asyncio
async def test_auto_disable_does_not_trigger_for_sonnet(tmp_path: Path) -> None:
    """Sanity check: with a sonnet judge, the tournament still runs."""
    from test_plan_phase_with_tournament import (
        PlanTournamentStubAdapter,
    )

    adapter = PlanTournamentStubAdapter(judge_favor="A")
    orch = _orch_with_judge_model(
        tmp_path, adapter, judge_model="sonnet", auto_disable=["opus"]
    )

    await orch.plan("Add foo(x)")

    assert adapter._n.get("judge", 0) >= 1

    ledger = await orch.plan_manager.read_ledger()
    assert any(e.op == "plan_tournament_complete" for e in ledger)
