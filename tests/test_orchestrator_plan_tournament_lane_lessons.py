"""v0.18.0 B1: lane tag is threaded into plan-tournament lesson emission."""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from agents import build_registry
from config.defaults import default_config
from config.schema import BranchConfig
from orchestrator import Orchestrator
from orchestrator.plan_tournament_runner import _emit_plan_tournament_lessons
from state.schemas import Plan
from tournament import PassResult

from stub_adapter import StubAdapter


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _orch(tmp_path: Path) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    return Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=StubAdapter({}),
        registry=build_registry(cfg),
        session_id="sess",
    )


def _mk_plan() -> Plan:
    return Plan(
        plan_id="p", spec_hash="h",
        phases=[],
        created_at=_iso(), updated_at=_iso(),
    )


def _mk_history() -> list[PassResult]:
    return [
        PassResult(
            pass_num=1, winner="A",
            scores={"A": 3, "B": 2, "AB": 1},
            valid_judges=1, elapsed_s=0.01, judge_details=[],
            incumbent_hash_before="h0", incumbent_hash_after="h1",
            meta={},
        )
    ]


@pytest.mark.asyncio
async def test_plan_tournament_lessons_no_branch_config_lane_is_none(
    tmp_path: Path,
) -> None:
    """Without branch_config, emitted lessons have no lane tag."""
    orch = _orch(tmp_path)
    await orch.plan_manager.init_plan(_mk_plan())

    await _emit_plan_tournament_lessons(
        orch,
        tournament_id="plan-deadbeef",
        history=_mk_history(),
        final_md="# plan\n",
        initial_md="# initial\n",
        branch_index=None,
    )

    entries = await orch.knowledge.read_all(tier="swarm")
    assert len(entries) > 0
    for e in entries:
        assert "lane" not in e.metadata


@pytest.mark.asyncio
async def test_plan_tournament_lessons_with_branch_config_tags_lane(
    tmp_path: Path,
) -> None:
    """With branch_config, emitted lessons carry metadata['lane']=branch_config.lane."""
    orch = _orch(tmp_path)
    await orch.plan_manager.init_plan(_mk_plan())

    bc = BranchConfig(lane="distant-scout")
    await _emit_plan_tournament_lessons(
        orch,
        tournament_id="plan-deadbeef",
        history=_mk_history(),
        final_md="# plan\n",
        initial_md="# initial\n",
        branch_index=0,
        branch_config=bc,
    )

    entries = await orch.knowledge.read_all(tier="swarm")
    assert len(entries) > 0
    for e in entries:
        assert e.metadata.get("lane") == "distant-scout"
