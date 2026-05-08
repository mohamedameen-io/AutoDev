"""Tests for v0.18.0 C1 — VetoAggregator wiring in ``run_impl_tournament``."""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from adapters.types import AgentResult
from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator.impl_tournament_runner import run_impl_tournament
from state.schemas import Phase, Plan, Task
from tournament import ImplBundle
from tournament.voting import BordaAggregator, VetoAggregator

from stub_adapter import StubAdapter


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan() -> Plan:
    return Plan(
        plan_id="p-veto",
        spec_hash="h",
        phases=[
            Phase(
                id="1",
                title="Work",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="Task",
                        description="Do it",
                    )
                ],
            )
        ],
        created_at=_iso(),
        updated_at=_iso(),
    )


def _stub_adapter() -> StubAdapter:
    def _h(inv):
        if inv.role == "judge":
            return AgentResult(success=True, text="RANKING: 1, 2, 3", duration_s=0.01)
        return AgentResult(success=True, text=f"[{inv.role}]", duration_s=0.01)

    return StubAdapter({r: _h for r in (
        "developer", "test_engineer", "critic_t", "architect_b",
        "synthesizer", "judge",
    )})


def _orch(tmp_path: Path, voting_strategy: str = "borda") -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.impl.enabled = True
    cfg.tournaments.impl.num_judges = 1
    cfg.tournaments.impl.convergence_k = 1
    cfg.tournaments.impl.max_rounds = 1
    cfg.tournaments.impl.voting_strategy = voting_strategy  # type: ignore[assignment]
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.auto_disable_for_models = []
    cfg.agents["judge"].model = "sonnet"
    registry = build_registry(cfg)
    return Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=_stub_adapter(),
        registry=registry,
        session_id="sess",
    )


@pytest.mark.asyncio
async def test_default_voting_strategy_is_borda(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``voting_strategy="borda"`` (default), the runner constructs an
    ImplTournament with a BordaAggregator instance."""
    import subprocess
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"],
                   cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"],
                   cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "README.md").write_text("init")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"],
                   cwd=tmp_path, check=True, capture_output=True)

    orch = _orch(tmp_path, voting_strategy="borda")
    await orch.plan_manager.init_plan(_mk_plan())
    task = await orch.plan_manager.get_task("1.1")
    assert task is not None

    captured: dict = {}

    class _Capturing:
        def __init__(self, *, voting_strategy=None, **_kw):
            captured["voting_strategy"] = voting_strategy

        async def run(self, *, task_prompt, initial):
            return initial, []

    from orchestrator import impl_tournament_runner as itr

    monkeypatch.setattr(itr, "ImplTournament", _Capturing)

    initial = ImplBundle(
        task_id="1.1", task_description="x", diff="", files_changed=[],
        tests_passed=0, tests_failed=0, tests_total=0,
        test_output_excerpt="",
    )
    await run_impl_tournament(orch, task, initial)
    # ``None`` (default) — runner did not override the strategy.
    assert captured["voting_strategy"] is None


@pytest.mark.asyncio
async def test_veto_voting_strategy_constructs_veto_aggregator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``voting_strategy="veto"``, the runner constructs a VetoAggregator."""
    import subprocess
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"],
                   cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"],
                   cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "README.md").write_text("init")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"],
                   cwd=tmp_path, check=True, capture_output=True)

    orch = _orch(tmp_path, voting_strategy="veto")
    await orch.plan_manager.init_plan(_mk_plan())
    task = await orch.plan_manager.get_task("1.1")
    assert task is not None

    captured: dict = {}

    class _Capturing:
        def __init__(self, *, voting_strategy=None, **_kw):
            captured["voting_strategy"] = voting_strategy

        async def run(self, *, task_prompt, initial):
            return initial, []

    from orchestrator import impl_tournament_runner as itr

    monkeypatch.setattr(itr, "ImplTournament", _Capturing)

    initial = ImplBundle(
        task_id="1.1", task_description="x", diff="", files_changed=[],
        tests_passed=0, tests_failed=0, tests_total=0,
        test_output_excerpt="",
    )
    await run_impl_tournament(orch, task, initial)
    assert isinstance(captured["voting_strategy"], VetoAggregator)


def test_tournament_default_voting_strategy_is_borda() -> None:
    """Tournament(__init__) defaults voting_strategy to BordaAggregator."""
    from tournament.core import Tournament, TournamentConfig
    from tournament import PlanContentHandler

    class _NoopClient:
        async def call(self, **_kw):
            return ""

    t = Tournament(
        handler=PlanContentHandler(),
        client=_NoopClient(),
        cfg=TournamentConfig(),
        artifact_dir=Path("/tmp"),
    )
    assert isinstance(t.voting_strategy, BordaAggregator)


def test_tournament_accepts_explicit_voting_strategy() -> None:
    """Tournament accepts a VetoAggregator instance via voting_strategy."""
    from tournament.core import Tournament, TournamentConfig
    from tournament import PlanContentHandler

    class _NoopClient:
        async def call(self, **_kw):
            return ""

    veto = VetoAggregator()
    t = Tournament(
        handler=PlanContentHandler(),
        client=_NoopClient(),
        cfg=TournamentConfig(),
        artifact_dir=Path("/tmp"),
        voting_strategy=veto,
    )
    assert t.voting_strategy is veto
