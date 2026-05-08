"""v0.18.0 A2: tests for run_multi_branch_phase_review_tournament."""

from __future__ import annotations

import datetime as _dt
import subprocess
from pathlib import Path

import pytest

from adapters.types import AgentResult
from agents import build_registry
from config.defaults import default_config
from config.schema import BranchConfig
from errors import TournamentError
from orchestrator import Orchestrator
from orchestrator.phase_review_runner import (
    PhaseReviewOutcome,
    run_multi_branch_phase_review_tournament,
)
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


def _mk_plan() -> tuple[Plan, Phase]:
    plan = Plan(
        plan_id="p", spec_hash="h",
        phases=[Phase(id="1", title="x", tasks=[
            Task(id="1.1", phase_id="1", title="t", description="t",
                 status="complete"),
        ])],
        created_at=_iso(), updated_at=_iso(),
    )
    return plan, plan.phases[0]


def _patch_outcomes(monkeypatch, outcomes_per_call: list[PhaseReviewOutcome | Exception]):
    """Patch run_phase_review_tournament to return one of the canned outcomes per call."""
    from orchestrator import phase_review_runner as prr

    call_count = {"i": 0}

    async def fake(**kwargs):
        idx = call_count["i"]
        call_count["i"] += 1
        outcome = outcomes_per_call[idx]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(prr, "run_phase_review_tournament", fake)


@pytest.mark.asyncio
async def test_three_branches_run_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3 branches all succeed, all accept_phase=True → meta accept."""
    _git_init(tmp_path)
    orch = _orch(tmp_path)
    plan, phase = _mk_plan()
    await orch.plan_manager.init_plan(plan)

    accept_outcome = PhaseReviewOutcome(
        winner="A", accept_phase=True, corrective_direction=None, history=[],
    )
    _patch_outcomes(monkeypatch, [accept_outcome] * 3)

    out = await run_multi_branch_phase_review_tournament(
        orch=orch, phase=phase,
        baseline_commit="dead", tip_commit="dead", spec_md="spec",
        n_branches=3,
    )
    assert out.accept_phase is True
    assert out.winner == "A"
    assert out.corrective_direction is None


@pytest.mark.asyncio
async def test_majority_vote_on_accept_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2 of 3 reject → majority reject."""
    _git_init(tmp_path)
    orch = _orch(tmp_path)
    plan, phase = _mk_plan()
    await orch.plan_manager.init_plan(plan)

    reject_b = PhaseReviewOutcome(
        winner="B", accept_phase=False,
        corrective_direction="- fix bug X", history=[],
    )
    reject_ab = PhaseReviewOutcome(
        winner="AB", accept_phase=False,
        corrective_direction="- handle edge case Y", history=[],
    )
    accept_outcome = PhaseReviewOutcome(
        winner="A", accept_phase=True, corrective_direction=None, history=[],
    )
    _patch_outcomes(monkeypatch, [reject_b, reject_ab, accept_outcome])

    out = await run_multi_branch_phase_review_tournament(
        orch=orch, phase=phase,
        baseline_commit="dead", tip_commit="dead", spec_md="spec",
        n_branches=3,
    )
    assert out.accept_phase is False
    assert out.corrective_direction is not None
    assert "fix bug X" in out.corrective_direction
    assert "handle edge case Y" in out.corrective_direction


@pytest.mark.asyncio
async def test_corrective_text_dedup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Duplicate corrective lines across branches are deduped."""
    _git_init(tmp_path)
    orch = _orch(tmp_path)
    plan, phase = _mk_plan()
    await orch.plan_manager.init_plan(plan)

    same_correction = PhaseReviewOutcome(
        winner="B", accept_phase=False,
        corrective_direction="- normalize input\n- handle errors",
        history=[],
    )
    same_correction2 = PhaseReviewOutcome(
        winner="B", accept_phase=False,
        corrective_direction="- normalize input\n- add logging",
        history=[],
    )
    _patch_outcomes(monkeypatch, [same_correction, same_correction2])

    out = await run_multi_branch_phase_review_tournament(
        orch=orch, phase=phase,
        baseline_commit="dead", tip_commit="dead", spec_md="spec",
        n_branches=2,
    )
    assert out.accept_phase is False
    assert out.corrective_direction is not None
    text = out.corrective_direction
    # "normalize input" appears once, not twice.
    assert text.count("normalize input") == 1
    assert "handle errors" in text
    assert "add logging" in text


@pytest.mark.asyncio
async def test_one_of_three_failure_meta_merges_survivors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1 branch raises, 2 survive → meta-merge over the 2 survivors."""
    _git_init(tmp_path)
    orch = _orch(tmp_path)
    plan, phase = _mk_plan()
    await orch.plan_manager.init_plan(plan)

    accept_outcome = PhaseReviewOutcome(
        winner="A", accept_phase=True, corrective_direction=None, history=[],
    )
    _patch_outcomes(monkeypatch, [
        accept_outcome,
        RuntimeError("branch 2 crashed"),
        accept_outcome,
    ])

    out = await run_multi_branch_phase_review_tournament(
        orch=orch, phase=phase,
        baseline_commit="dead", tip_commit="dead", spec_md="spec",
        n_branches=3,
    )
    assert out.accept_phase is True


@pytest.mark.asyncio
async def test_two_of_three_failure_below_floor_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2 branches fail; only 1 survivor < floor=2 → TournamentError."""
    _git_init(tmp_path)
    orch = _orch(tmp_path)
    plan, phase = _mk_plan()
    await orch.plan_manager.init_plan(plan)

    accept_outcome = PhaseReviewOutcome(
        winner="A", accept_phase=True, corrective_direction=None, history=[],
    )
    _patch_outcomes(monkeypatch, [
        RuntimeError("a"),
        RuntimeError("b"),
        accept_outcome,
    ])

    with pytest.raises(TournamentError, match="survivor floor"):
        await run_multi_branch_phase_review_tournament(
            orch=orch, phase=phase,
            baseline_commit="dead", tip_commit="dead", spec_md="spec",
            n_branches=3,
        )


@pytest.mark.asyncio
async def test_n_branches_one_passes_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """n_branches=1 short-circuits to the single-branch path."""
    _git_init(tmp_path)
    orch = _orch(tmp_path)
    plan, phase = _mk_plan()
    await orch.plan_manager.init_plan(plan)

    accept_outcome = PhaseReviewOutcome(
        winner="A", accept_phase=True, corrective_direction=None, history=[],
    )
    _patch_outcomes(monkeypatch, [accept_outcome])

    out = await run_multi_branch_phase_review_tournament(
        orch=orch, phase=phase,
        baseline_commit="dead", tip_commit="dead", spec_md="spec",
        n_branches=1,
    )
    assert out.accept_phase is True


@pytest.mark.asyncio
async def test_branch_configs_length_mismatch_raises(tmp_path: Path) -> None:
    """branch_configs length != n_branches → ValueError."""
    _git_init(tmp_path)
    orch = _orch(tmp_path)
    plan, phase = _mk_plan()
    await orch.plan_manager.init_plan(plan)

    with pytest.raises(ValueError, match="must equal"):
        await run_multi_branch_phase_review_tournament(
            orch=orch, phase=phase,
            baseline_commit="dead", tip_commit="dead", spec_md="spec",
            n_branches=3,
            branch_configs=[BranchConfig(lane="local-tweak")],
        )


@pytest.mark.asyncio
async def test_ledger_breadcrumbs_emitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """multi_branch_phase_review_start/meta_merge_complete/complete entries."""
    _git_init(tmp_path)
    orch = _orch(tmp_path)
    plan, phase = _mk_plan()
    await orch.plan_manager.init_plan(plan)

    accept_outcome = PhaseReviewOutcome(
        winner="A", accept_phase=True, corrective_direction=None, history=[],
    )
    _patch_outcomes(monkeypatch, [accept_outcome] * 3)

    await run_multi_branch_phase_review_tournament(
        orch=orch, phase=phase,
        baseline_commit="dead", tip_commit="dead", spec_md="spec",
        n_branches=3,
    )

    ledger = await orch.plan_manager.read_ledger()
    ops = [e.op for e in ledger]
    assert "multi_branch_phase_review_start" in ops
    assert "multi_branch_phase_review_meta_merge_complete" in ops
    assert "multi_branch_phase_review_complete" in ops
