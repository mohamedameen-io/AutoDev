"""Tests for the impl-tournament auto-disable path.

When ``cfg.tournaments.auto_disable_for_models`` matches the resolved
tournament model (e.g. the judge is an opus-tier model), the impl tournament
must skip entirely — no tournament subprocess calls, no evidence written.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator.impl_tournament_runner import (
    _is_auto_disabled,
    _resolve_tournament_model,
    run_impl_tournament,
)
from tournament import ImplBundle

from stub_adapter import StubAdapter


INITIAL_BUNDLE = ImplBundle(
    task_id="1.1",
    task_description="Add foo()",
    diff="+def foo(): pass",
    files_changed=["foo.py"],
    tests_passed=3,
    tests_failed=0,
    tests_total=3,
    test_output_excerpt="3 passed",
)


def _orch_with_judge_model(
    cwd: Path,
    adapter: StubAdapter,
    *,
    judge_model: str,
    auto_disable: list[str] | None = None,
) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.impl.enabled = True
    cfg.tournaments.impl.num_judges = 1
    cfg.tournaments.impl.convergence_k = 1
    cfg.tournaments.impl.max_rounds = 3
    if auto_disable is not None:
        cfg.tournaments.auto_disable_for_models = auto_disable
    cfg.tournaments.plan.enabled = False
    cfg.agents["judge"].model = judge_model
    registry = build_registry(cfg)
    return Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-test-impl-autodisable",
    )


# ── Pure-function tests ─────────────────────────────────────────────────


def test_is_auto_disabled_exact_match() -> None:
    assert _is_auto_disabled("opus", ["opus"]) is True


def test_is_auto_disabled_substring_match() -> None:
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


# ── End-to-end: run_impl_tournament is a no-op when auto-disabled ───────


@pytest.mark.asyncio
async def test_run_impl_tournament_returns_initial_when_auto_disabled(
    tmp_path: Path,
) -> None:
    """``run_impl_tournament`` short-circuits: no tournament calls, no evidence."""
    adapter = StubAdapter({})
    orch = _orch_with_judge_model(
        tmp_path, adapter, judge_model="opus", auto_disable=["opus"]
    )

    # Need a minimal plan so ledger_append works.
    import datetime as _dt
    from state.schemas import Phase, Plan, Task

    plan = Plan(
        plan_id="p-test",
        spec_hash="x",
        phases=[
            Phase(
                id="1",
                title="Work",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="Add foo",
                        description="Add foo()",
                    )
                ],
            )
        ],
        created_at=_dt.datetime.now(_dt.timezone.utc).isoformat(),
        updated_at=_dt.datetime.now(_dt.timezone.utc).isoformat(),
    )
    await orch.plan_manager.init_plan(plan)
    task = await orch.plan_manager.get_task("1.1")
    assert task is not None

    result = await run_impl_tournament(orch, task, INITIAL_BUNDLE)
    assert result is INITIAL_BUNDLE

    # No tournament roles invoked.
    assert adapter.count("critic_t") == 0
    assert adapter.count("architect_b") == 0
    assert adapter.count("judge") == 0

    # No tournament evidence written.
    ev_path = tmp_path / ".autodev" / "evidence" / "1.1-tournament.json"
    assert not ev_path.exists()

    # No ledger entry.
    ledger = await orch.plan_manager.read_ledger()
    assert all(e.op != "impl_tournament_complete" for e in ledger)
