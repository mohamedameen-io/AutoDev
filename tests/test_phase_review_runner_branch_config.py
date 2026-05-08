"""v0.18.0 A1: branch_config threading in run_phase_review_tournament."""

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
from orchestrator.phase_review_runner import run_phase_review_tournament
from state.schemas import Phase, Plan, Task

from stub_adapter import StubAdapter


def _git_init(path: Path) -> str:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"],
                   cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"],
                   cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("init")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"],
                   cwd=path, check=True, capture_output=True)
    rev = subprocess.run(["git", "rev-parse", "HEAD"],
                         cwd=path, check=True, capture_output=True, text=True)
    return rev.stdout.strip()


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _stub() -> StubAdapter:
    def _h(inv):
        if inv.role == "judge":
            return AgentResult(success=True, text="RANKING: 1, 2, 3", duration_s=0.01)
        return AgentResult(success=True, text=f"[{inv.role}]", duration_s=0.01)

    return StubAdapter({r: _h for r in (
        "developer", "test_engineer", "critic_t", "architect_b",
        "synthesizer", "judge", "critic_drift_verifier",
    )})


def _orch(tmp_path: Path) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.impl.enabled = False
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.phase_review.enabled = True
    cfg.tournaments.phase_review.num_judges = 1
    cfg.tournaments.phase_review.convergence_k = 1
    cfg.tournaments.phase_review.max_rounds = 1
    cfg.tournaments.phase_review.drift_verifier_enabled = False
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
async def test_phase_review_branch_config_threads_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """branch_config wires role_model_overrides + lane suffix into phase-review."""
    head = _git_init(tmp_path)
    orch = _orch(tmp_path)
    plan = Plan(
        plan_id="p", spec_hash="h",
        phases=[Phase(id="1", title="x", tasks=[
            Task(id="1.1", phase_id="1", title="t", description="t",
                 status="complete"),
        ])],
        created_at=_iso(), updated_at=_iso(),
    )
    await orch.plan_manager.init_plan(plan)
    phase = plan.phases[0]

    captured: dict = {}

    class _CapturingClient:
        def __init__(self, *args, **kwargs):
            captured["role_model_overrides"] = kwargs.get("role_model_overrides")

        async def call(self, **_kw):
            return ""

    class _CapturingTournament:
        def __init__(self, **kwargs):
            captured["artifact_dir"] = kwargs["artifact_dir"]
            self._initial = None

        async def run(self, *, task_prompt, initial):
            self._initial = initial
            from tournament import PassResult
            return initial, [
                PassResult(
                    pass_num=1, winner="A",
                    scores={"A": 3, "B": 2, "AB": 1},
                    valid_judges=1, elapsed_s=0.01, judge_details=[],
                    incumbent_hash_before="x", incumbent_hash_after="x",
                    meta={},
                )
            ]

    from orchestrator import phase_review_runner as prr

    monkeypatch.setattr(prr, "AdapterLLMClient", _CapturingClient)
    monkeypatch.setattr(prr, "Tournament", _CapturingTournament)

    bc = BranchConfig(
        model_overrides={"judge": "haiku"},
        lane="architectural",
    )

    await run_phase_review_tournament(
        orch=orch,
        phase=phase,
        baseline_commit=head,
        tip_commit=head,
        spec_md="spec",
        branch_config=bc,
    )

    assert captured["role_model_overrides"] == {"judge": "haiku"}
    assert str(captured["artifact_dir"]).endswith("-architectural")


@pytest.mark.asyncio
async def test_phase_review_default_no_branch_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When branch_config is None (default), no role_model_overrides set."""
    head = _git_init(tmp_path)
    orch = _orch(tmp_path)
    plan = Plan(
        plan_id="p", spec_hash="h",
        phases=[Phase(id="1", title="x", tasks=[
            Task(id="1.1", phase_id="1", title="t", description="t",
                 status="complete"),
        ])],
        created_at=_iso(), updated_at=_iso(),
    )
    await orch.plan_manager.init_plan(plan)
    phase = plan.phases[0]

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
            from tournament import PassResult
            return initial, [
                PassResult(
                    pass_num=1, winner="A",
                    scores={"A": 3, "B": 2, "AB": 1},
                    valid_judges=1, elapsed_s=0.01, judge_details=[],
                    incumbent_hash_before="x", incumbent_hash_after="x",
                    meta={},
                )
            ]

    from orchestrator import phase_review_runner as prr

    monkeypatch.setattr(prr, "AdapterLLMClient", _CapturingClient)
    monkeypatch.setattr(prr, "Tournament", _CapturingTournament)

    await run_phase_review_tournament(
        orch=orch, phase=phase,
        baseline_commit=head, tip_commit=head, spec_md="spec",
    )

    assert captured["role_model_overrides"] is None
    assert "-distant-scout" not in str(captured["artifact_dir"])
