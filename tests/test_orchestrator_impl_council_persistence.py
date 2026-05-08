"""v0.18.0 C2: council sidecar JSON persistence in impl_tournament_runner."""

from __future__ import annotations

import datetime as _dt
import json
import subprocess
from pathlib import Path

import pytest

from adapters.types import AgentResult
from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator.impl_tournament_runner import run_impl_tournament
from state.paths import council_criteria_path
from state.schemas import AcceptanceCriterion, Phase, Plan, Task
from tournament import ImplBundle

from stub_adapter import StubAdapter


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"],
                   cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"],
                   cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("init")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"],
                   cwd=path, check=True, capture_output=True)


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _stub() -> StubAdapter:
    def _h(inv):
        if inv.role == "judge":
            return AgentResult(success=True, text="RANKING: 1, 2, 3", duration_s=0.01)
        return AgentResult(success=True, text=f"[{inv.role}]", duration_s=0.01)

    return StubAdapter({r: _h for r in (
        "developer", "test_engineer", "critic_t", "architect_b",
        "synthesizer", "judge",
    )})


def _orch(tmp_path: Path) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.impl.enabled = True
    cfg.tournaments.impl.num_judges = 1
    cfg.tournaments.impl.convergence_k = 1
    cfg.tournaments.impl.max_rounds = 1
    cfg.tournaments.impl.voting_strategy = "veto"
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.auto_disable_for_models = []
    cfg.agents["judge"].model = "sonnet"
    return Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=_stub(),
        registry=build_registry(cfg),
        session_id="sess",
    )


@pytest.mark.asyncio
async def test_council_sidecar_persists_criteria(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When voting_strategy=veto + task has acceptance, a council sidecar is written."""
    _git_init(tmp_path)
    orch = _orch(tmp_path)
    task = Task(
        id="1.1", phase_id="1", title="t", description="d",
        acceptance=[
            AcceptanceCriterion(id="ac1", description="must compile"),
            AcceptanceCriterion(id="ac2", description="tests pass"),
        ],
    )
    plan = Plan(
        plan_id="p", spec_hash="h",
        phases=[Phase(id="1", title="x", tasks=[task])],
        created_at=_iso(), updated_at=_iso(),
    )
    await orch.plan_manager.init_plan(plan)
    task_loaded = await orch.plan_manager.get_task("1.1")
    assert task_loaded is not None

    class _Capturing:
        def __init__(self, **kwargs):
            self._initial = None

        async def run(self, *, task_prompt, initial):
            return initial, []

    from orchestrator import impl_tournament_runner as itr

    monkeypatch.setattr(itr, "ImplTournament", _Capturing)

    initial = ImplBundle(
        task_id="1.1", task_description="d", diff="", files_changed=[],
        tests_passed=0, tests_failed=0, tests_total=0, test_output_excerpt="",
    )
    await run_impl_tournament(orch, task_loaded, initial)

    sidecar = council_criteria_path(tmp_path, "1.1")
    assert sidecar.exists()
    data = json.loads(sidecar.read_text())
    assert data["task_id"] == "1.1"
    assert len(data["criteria"]) == 2
    assert data["criteria"][0]["id"] == "ac1"
    assert data["criteria"][0]["description"] == "must compile"


@pytest.mark.asyncio
async def test_council_sidecar_skipped_when_borda(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When voting_strategy=borda (default), no council sidecar is written."""
    _git_init(tmp_path)
    orch = _orch(tmp_path)
    orch.cfg.tournaments.impl.voting_strategy = "borda"

    task = Task(
        id="1.1", phase_id="1", title="t", description="d",
        acceptance=[AcceptanceCriterion(id="ac1", description="x")],
    )
    plan = Plan(
        plan_id="p", spec_hash="h",
        phases=[Phase(id="1", title="x", tasks=[task])],
        created_at=_iso(), updated_at=_iso(),
    )
    await orch.plan_manager.init_plan(plan)
    task_loaded = await orch.plan_manager.get_task("1.1")
    assert task_loaded is not None

    class _Capturing:
        def __init__(self, **kwargs):
            pass

        async def run(self, *, task_prompt, initial):
            return initial, []

    from orchestrator import impl_tournament_runner as itr

    monkeypatch.setattr(itr, "ImplTournament", _Capturing)

    initial = ImplBundle(
        task_id="1.1", task_description="d", diff="", files_changed=[],
        tests_passed=0, tests_failed=0, tests_total=0, test_output_excerpt="",
    )
    await run_impl_tournament(orch, task_loaded, initial)

    sidecar = council_criteria_path(tmp_path, "1.1")
    assert not sidecar.exists()
