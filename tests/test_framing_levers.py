"""Minimality lever-suspension tests for the altitude panel (Phase 4).

The five minimality levers are suspended SCOPED to the altitude_judge panel and revert
for the downstream plan tournament. Suspension is achieved by what the panel does NOT do:
it dispatches ``altitude_judge`` only (never ``minimality_judge``/``judge``), uses its own
prompt (never JUDGE_RANK_3), never demotes oversized winners, and relies on the
``denylist_roles`` entry to keep ``anti_bloat_v1`` out of the cohort.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator.framing_phase import run_framing_phase
from stub_adapter import StubAdapter, ok

from test_framing_phase import (
    _design_text,
    _judge_ranks_design_first,
)


def _bootstrap_repo(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=str(tmp_path), check=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(tmp_path), check=True)
    (tmp_path / "main.py").write_text("def main():\n    return 1\n")
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(tmp_path), check=True)


def _make_orch(cwd: Path, adapter: StubAdapter) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    return Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=build_registry(cfg),
        session_id="sess-levers",
    )


def _force_structural(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake(cwd: object, candidate_digest: object, intent: str):
        return ["recurrence_at_seam"], True

    monkeypatch.setattr("orchestrator.framing_phase._compute_signals", _fake)


def _design_adapter() -> StubAdapter:
    return StubAdapter(
        {"framing": ok(_design_text()), "altitude_judge": _judge_ranks_design_first}
    )


@pytest.mark.asyncio
async def test_altitude_judge_never_loads_minimality_judge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bootstrap_repo(tmp_path)
    _force_structural(monkeypatch)
    adapter = _design_adapter()
    orch = _make_orch(tmp_path, adapter)
    await run_framing_phase(orch, "trim it", "", "", None, "abc123")
    assert adapter.count("minimality_judge") == 0


@pytest.mark.asyncio
async def test_anti_bloat_not_injected_into_framing_cohort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    denylist = default_config().knowledge.denylist_roles
    assert "framing" in denylist
    assert "altitude_judge" in denylist
    _bootstrap_repo(tmp_path)
    _force_structural(monkeypatch)
    adapter = _design_adapter()
    orch = _make_orch(tmp_path, adapter)
    await run_framing_phase(orch, "trim it", "", "", None, "abc123")
    # The panel dispatches the bare altitude_judge prompt via the specialist path,
    # so no anti_bloat seed lessons / length-penalty text leak into the cohort.
    for prompt in adapter.prompts_for("altitude_judge"):
        low = prompt.lower()
        assert "anti_bloat" not in low
        assert "mandatory length penalty" not in low


@pytest.mark.asyncio
async def test_panel_dispatches_altitude_judge_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bootstrap_repo(tmp_path)
    _force_structural(monkeypatch)
    adapter = _design_adapter()
    orch = _make_orch(tmp_path, adapter)
    await run_framing_phase(orch, "trim it", "", "", None, "abc123")
    roles = {c.role for c in adapter.calls}
    assert roles <= {"framing", "altitude_judge"}
    assert "judge" not in roles
    assert "minimality_judge" not in roles
