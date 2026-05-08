"""v0.18.0 A1: branch_config threading in run_impl_tournament + run_phase_review_tournament."""

from __future__ import annotations

import datetime as _dt
import subprocess
from pathlib import Path

import pytest

from adapters.types import AgentResult
from agents import build_registry
from config.defaults import default_config
from config.schema import BranchConfig
from orchestrator import Orchestrator
from orchestrator.impl_tournament_runner import run_impl_tournament
from state.schemas import Phase, Plan, Task
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


def _mk_plan() -> Plan:
    return Plan(
        plan_id="p-bc",
        spec_hash="h",
        phases=[
            Phase(
                id="1",
                title="Work",
                tasks=[Task(id="1.1", phase_id="1", title="x", description="x")],
            )
        ],
        created_at=_iso(),
        updated_at=_iso(),
    )


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
async def test_run_impl_tournament_default_no_branch_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When branch_config is None (default), no role_model_overrides are set."""
    _git_init(tmp_path)
    orch = _orch(tmp_path)
    await orch.plan_manager.init_plan(_mk_plan())
    task = await orch.plan_manager.get_task("1.1")
    assert task is not None

    captured: dict = {}

    class _CapturingClient:
        def __init__(self, *args, **kwargs):
            captured["role_model_overrides"] = kwargs.get("role_model_overrides")

        async def call(self, **_kw):
            return ""

    class _CapturingTournament:
        def __init__(self, **kwargs):
            captured["artifact_dir"] = kwargs["artifact_dir"]

        async def run(self, *, task_prompt, initial):
            return initial, []

    from orchestrator import impl_tournament_runner as itr

    monkeypatch.setattr(itr, "AdapterLLMClient", _CapturingClient)
    monkeypatch.setattr(itr, "ImplTournament", _CapturingTournament)

    initial = ImplBundle(
        task_id="1.1", task_description="x", diff="", files_changed=[],
        tests_passed=0, tests_failed=0, tests_total=0, test_output_excerpt="",
    )
    await run_impl_tournament(orch, task, initial)
    assert captured["role_model_overrides"] is None
    # artifact dir has no lane suffix
    assert "-distant-scout" not in str(captured["artifact_dir"])
    assert "-local-tweak" not in str(captured["artifact_dir"])


@pytest.mark.asyncio
async def test_run_impl_tournament_with_branch_config_threads_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When branch_config is set, role_model_overrides + lane suffix are wired."""
    _git_init(tmp_path)
    orch = _orch(tmp_path)
    await orch.plan_manager.init_plan(_mk_plan())
    task = await orch.plan_manager.get_task("1.1")
    assert task is not None

    captured: dict = {}

    class _CapturingClient:
        def __init__(self, *args, **kwargs):
            captured["role_model_overrides"] = kwargs.get("role_model_overrides")

        async def call(self, **_kw):
            return ""

    class _CapturingTournament:
        def __init__(self, **kwargs):
            captured["artifact_dir"] = kwargs["artifact_dir"]

        async def run(self, *, task_prompt, initial):
            return initial, []

    from orchestrator import impl_tournament_runner as itr

    monkeypatch.setattr(itr, "AdapterLLMClient", _CapturingClient)
    monkeypatch.setattr(itr, "ImplTournament", _CapturingTournament)

    bc = BranchConfig(
        model_overrides={"developer": "sonnet-3.5", "judge": "haiku-3.5"},
        lane="distant-scout",
    )

    initial = ImplBundle(
        task_id="1.1", task_description="x", diff="", files_changed=[],
        tests_passed=0, tests_failed=0, tests_total=0, test_output_excerpt="",
    )
    await run_impl_tournament(orch, task, initial, branch_config=bc)
    assert captured["role_model_overrides"] == {
        "developer": "sonnet-3.5",
        "judge": "haiku-3.5",
    }
    # artifact dir has lane suffix
    assert str(captured["artifact_dir"]).endswith("-distant-scout")
