"""v0.18.0 B2: integration test for plateau-detection + lane forcing."""

from __future__ import annotations

from pathlib import Path

import pytest

from agents import build_registry
from config.defaults import default_config
from config.schema import BranchConfig
from orchestrator import Orchestrator
from orchestrator.multi_branch_tournament import run_multi_branch_plan_tournament
from state.knowledge import TournamentEvent

from stub_adapter import StubAdapter


def _orch(tmp_path: Path) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = True
    cfg.tournaments.plan.num_judges = 1
    cfg.tournaments.plan.convergence_k = 1
    cfg.tournaments.plan.max_rounds = 1
    cfg.tournaments.plan.plateau_detection_enabled = True
    cfg.tournaments.plan.plateau_window = 4
    cfg.repeated_hypothesis_threshold = 0  # disable so it doesn't tag branches
    cfg.tournaments.auto_disable_for_models = []
    cfg.agents["judge"].model = "sonnet"
    return Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=StubAdapter({}),
        registry=build_registry(cfg),
        session_id="sess",
    )


@pytest.mark.asyncio
async def test_plateau_forces_distant_scout_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When per-family plateau detected, one branch's lane forced to distant-scout."""
    orch = _orch(tmp_path)
    # Disable dedup to allow the test events to all land.
    orch.knowledge.knowledge_config.dedup_threshold = 1.01

    # Seed swarm with a plateau for family "alpha".
    distinct = [
        "alpha bravo charlie delta echo",
        "foxtrot golf hotel india juliet",
        "kilo lima mike november oscar",
    ]
    for i in range(3):
        await orch.knowledge.record_tournament_event(TournamentEvent(
            event_type="discard",
            family="alpha",
            hypothesis=f"discard {i}: {distinct[i]} unique structure",
            evidence=f"failure {i}: {distinct[i]} forensic detail extra",
        ))

    # Patch _run_one_branch to capture the actual branch_config lane used.
    captured_lanes: list[str | None] = []

    async def _fake_branch(orch, initial_md, spec, spec_hash, *,
                           branch_index, branch_seed, branch_config=None):
        captured_lanes.append(
            branch_config.lane if branch_config else None
        )
        # Return distinct plan markdown per branch (so meta-merge runs).
        return f"# Plan branch {branch_index}\n"

    from orchestrator import multi_branch_tournament as mbt

    monkeypatch.setattr(mbt, "_run_one_branch", _fake_branch)

    # Bypass meta-merge by stubbing it out.
    async def _fake_meta_merge(orch, candidates, spec, spec_hash):
        return candidates[0], []

    monkeypatch.setattr(mbt, "_meta_merge_pairwise", _fake_meta_merge)

    # 2 branches, one with family alpha (plateaued) and one with beta.
    branch_configs = [
        BranchConfig(lane="local-tweak", family="alpha"),
        BranchConfig(lane="local-tweak", family="beta"),
    ]

    spec_hash = "abcdef0123456789"
    await run_multi_branch_plan_tournament(
        orch=orch,
        initial_md="# Initial\n",
        spec="spec",
        spec_hash=spec_hash,
        n_branches=2,
        branch_configs=branch_configs,
    )

    # Branch with family alpha should have been flipped to distant-scout.
    assert "distant-scout" in captured_lanes

    # Verify ledger ops emitted.
    ledger = await orch.plan_manager.read_ledger()
    ops = [e.op for e in ledger]
    assert "plateau_detected" in ops
    assert "plateau_forced_lane_change" in ops


@pytest.mark.asyncio
async def test_plateau_disabled_no_lane_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When plateau_detection_enabled=False, no lane mutation."""
    orch = _orch(tmp_path)
    orch.cfg.tournaments.plan.plateau_detection_enabled = False
    orch.knowledge.knowledge_config.dedup_threshold = 1.01

    distinct = [
        "alpha bravo charlie delta echo",
        "foxtrot golf hotel india juliet",
        "kilo lima mike november oscar",
    ]
    for i in range(3):
        await orch.knowledge.record_tournament_event(TournamentEvent(
            event_type="discard",
            family="alpha",
            hypothesis=f"discard {i}: {distinct[i]} unique structure",
            evidence=f"failure {i}: {distinct[i]} forensic detail extra",
        ))

    captured_lanes: list[str | None] = []

    async def _fake_branch(orch, initial_md, spec, spec_hash, *,
                           branch_index, branch_seed, branch_config=None):
        captured_lanes.append(
            branch_config.lane if branch_config else None
        )
        return f"# Plan {branch_index}\n"

    async def _fake_meta_merge(orch, candidates, spec, spec_hash):
        return candidates[0], []

    from orchestrator import multi_branch_tournament as mbt

    monkeypatch.setattr(mbt, "_run_one_branch", _fake_branch)
    monkeypatch.setattr(mbt, "_meta_merge_pairwise", _fake_meta_merge)

    branch_configs = [
        BranchConfig(lane="local-tweak", family="alpha"),
        BranchConfig(lane="local-tweak", family="beta"),
    ]

    await run_multi_branch_plan_tournament(
        orch=orch,
        initial_md="# Initial\n",
        spec="spec",
        spec_hash="abcdef0123456789",
        n_branches=2,
        branch_configs=branch_configs,
    )

    # No distant-scout in captured lanes — the original local-tweak lanes
    # are preserved.
    assert "distant-scout" not in captured_lanes

    ledger = await orch.plan_manager.read_ledger()
    ops = [e.op for e in ledger]
    assert "plateau_detected" not in ops
