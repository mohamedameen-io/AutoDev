"""Tests for execute_phase with impl tournament wired in (Phase 7)."""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from adapters.types import AgentResult
from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from state.schemas import (
    AcceptanceCriterion,
    Phase,
    Plan,
    Task,
)

from stub_adapter import StubAdapter, ok


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan() -> Plan:
    return Plan(
        plan_id="p-exec-t7",
        spec_hash="d",
        phases=[
            Phase(
                id="1",
                title="Work",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="Add foo",
                        description="Implement foo()",
                        files=["foo.py"],
                        acceptance=[
                            AcceptanceCriterion(id="ac-1", description="tests pass"),
                        ],
                    )
                ],
            )
        ],
        created_at=_iso(),
        updated_at=_iso(),
    )


def _coder_ok() -> AgentResult:
    return AgentResult(
        success=True,
        text="wrote foo",
        diff="diff --git a/foo.py b/foo.py\n+def foo(): pass",
        files_changed=[Path("foo.py")],
        duration_s=0.1,
    )


def _reviewer_ok() -> AgentResult:
    return ok("APPROVED\n- clean")


def _test_ok() -> AgentResult:
    return ok("ran pytest\nRESULTS: passed=3 failed=0 total=3")


async def _make_orch(
    cwd: Path,
    adapter: StubAdapter,
    *,
    impl_enabled: bool = True,
    judge_model: str = "sonnet",
    auto_disable: list[str] | None = None,
) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = impl_enabled
    cfg.tournaments.impl.num_judges = 1
    cfg.tournaments.impl.convergence_k = 1
    cfg.tournaments.impl.max_rounds = 2
    cfg.tournaments.auto_disable_for_models = auto_disable or []
    cfg.agents["judge"].model = judge_model
    registry = build_registry(cfg)
    orch = Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-exec-t7",
    )
    await orch.plan_manager.init_plan(_mk_plan())
    return orch


@pytest.mark.asyncio
async def test_execute_with_impl_tournament_disabled_completes(
    tmp_path: Path,
) -> None:
    """With impl tournament disabled, execute still completes normally."""
    adapter = StubAdapter(
        {
            "developer": _coder_ok(),
            "reviewer": _reviewer_ok(),
            "test_engineer": _test_ok(),
        }
    )
    orch = await _make_orch(tmp_path, adapter, impl_enabled=False)
    tasks = await orch.execute()
    assert len(tasks) == 1
    assert tasks[0].status == "complete"


@pytest.mark.asyncio
async def test_execute_with_impl_tournament_auto_disabled_completes(
    tmp_path: Path,
) -> None:
    """With opus judge + auto_disable=["opus"], tournament is skipped but task completes."""
    adapter = StubAdapter(
        {
            "developer": _coder_ok(),
            "reviewer": _reviewer_ok(),
            "test_engineer": _test_ok(),
        }
    )
    orch = await _make_orch(
        tmp_path,
        adapter,
        impl_enabled=True,
        judge_model="opus",
        auto_disable=["opus"],
    )
    tasks = await orch.execute()
    assert len(tasks) == 1
    assert tasks[0].status == "complete"
    # No tournament evidence written.
    ev_path = tmp_path / ".autodev" / "evidence" / "1.1-tournament.json"
    assert not ev_path.exists()


@pytest.mark.asyncio
async def test_execute_with_impl_tournament_error_still_completes(
    tmp_path: Path,
) -> None:
    """If the impl tournament raises, the task still completes (error is swallowed)."""
    # We trigger the tournament by enabling it with a non-opus model.
    # The tournament will fail because there's no real git repo for worktrees,
    # but the execute phase should catch the error and continue.
    adapter = StubAdapter(
        {
            "developer": _coder_ok(),
            "reviewer": _reviewer_ok(),
            "test_engineer": _test_ok(),
        }
    )
    orch = await _make_orch(
        tmp_path,
        adapter,
        impl_enabled=True,
        judge_model="sonnet",
        auto_disable=[],
    )
    tasks = await orch.execute()
    # Task should still complete even if tournament errors.
    assert len(tasks) == 1
    assert tasks[0].status == "complete"


@pytest.mark.asyncio
async def test_execute_with_impl_tournament_disabled_flag(
    tmp_path: Path,
) -> None:
    """``disable_impl_tournament=True`` skips tournament even when cfg.impl.enabled."""
    adapter = StubAdapter(
        {
            "developer": _coder_ok(),
            "reviewer": _reviewer_ok(),
            "test_engineer": _test_ok(),
        }
    )
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = True
    cfg.tournaments.impl.num_judges = 1
    cfg.tournaments.impl.convergence_k = 1
    cfg.tournaments.impl.max_rounds = 2
    cfg.tournaments.auto_disable_for_models = []
    cfg.agents["judge"].model = "sonnet"
    registry = build_registry(cfg)
    orch = Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-exec-t7-disabled",
        disable_impl_tournament=True,
    )
    await orch.plan_manager.init_plan(_mk_plan())
    tasks = await orch.execute()
    assert len(tasks) == 1
    assert tasks[0].status == "complete"
    # No tournament evidence.
    ev_path = tmp_path / ".autodev" / "evidence" / "1.1-tournament.json"
    assert not ev_path.exists()
