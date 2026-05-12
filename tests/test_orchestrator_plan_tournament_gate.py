"""v0.27 Phase 5 (audit §5): post-tournament structural-validity gate.

After the plan tournament refines the architect markdown, the
orchestrator MUST run a structural-validity gate over the refined
output before it lands in ``plan.json``:

  1. ``parse_plan_markdown(refined_md)`` — refined output must be a
     parseable plan.
  2. ``validate_files_exist(plan, cwd)`` — every listed path either
     exists on disk OR is opted-out via ``[new]``.

A failure on either step logs the rejection,
``tournament_output_rejected_structurally`` records it in the ledger,
and the orchestrator falls back to the pre-tournament plan.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator

from stub_adapter import StubAdapter, ok


def _init_repo(cwd: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(cwd), check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=str(cwd), check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=str(cwd), check=True
    )
    (cwd / "src" / "math").mkdir(parents=True, exist_ok=True)
    (cwd / "src" / "math" / "__init__.py").write_text(
        "def add(a, b): return a + b\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "-A"], cwd=str(cwd), check=True)
    subprocess.run(
        ["git", "commit", "-qm", "init"], cwd=str(cwd), check=True
    )


_GOOD_PRE_TOURNAMENT_PLAN = """
# Plan: Add subtract

EDIT_SCOPE:
  - src/math

## Phase 1: Implement
  - Acceptance:
    - [ ] subtract function exported

### Task 1.1: Add subtract
  - Description: real file
  - Files: src/math/__init__.py
  - Acceptance:
    - [ ] subtract function exported
"""

_REFINED_PLAN_WITH_BAD_PATH = """
# Plan: Add subtract refined

EDIT_SCOPE:
  - src/math

## Phase 1: Implement
  - Acceptance:
    - [ ] subtract function exported

### Task 1.1: Add subtract
  - Description: refined plan introduced a bogus path
  - Files: src/math/__init__.py, src/math/this_does_not_exist.py
  - Acceptance:
    - [ ] subtract function exported
"""


@pytest.mark.asyncio
async def test_tournament_refined_invalid_paths_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tournament refines the architect's plan into one with a
    bogus path. The post-tournament gate catches it via
    ``validate_files_exist`` and the orchestrator falls back to the
    pre-tournament plan; a ``tournament_output_rejected_structurally``
    op records the rejection."""
    _init_repo(tmp_path)
    adapter = StubAdapter(
        {
            "explorer": ok("found"),
            "domain_expert": ok("ok"),
            "architect": ok(_GOOD_PRE_TOURNAMENT_PLAN),
        }
    )

    cfg = default_config()
    cfg.tournaments.plan.enabled = True
    cfg.tournaments.plan.num_branches = 1
    cfg.tournaments.impl.enabled = False
    registry = build_registry(cfg)
    orch = Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-tournament-gate",
    )

    # Stub the tournament to return our bad-path refined markdown.
    async def fake_run_plan_tournament(
        orch, plan_md, intent, spec_hash, **_k
    ):
        return _REFINED_PLAN_WITH_BAD_PATH

    from orchestrator import plan_phase as pp

    monkeypatch.setattr(pp, "run_plan_tournament", fake_run_plan_tournament)

    plan = await orch.plan("Add subtract")
    assert plan is not None
    # Fallback: the pre-tournament plan's title survives.
    assert plan.metadata["title"] == "Add subtract"

    from state.ledger import read_entries

    ledger = read_entries(tmp_path)
    rejections = [
        e for e in ledger if e.op == "tournament_output_rejected_structurally"
    ]
    assert len(rejections) == 1
    assert rejections[0].payload["reason"] == "validate_files_exist"


@pytest.mark.asyncio
async def test_tournament_refined_unparseable_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tournament that returns garbage markdown trips the parser
    leg of the gate."""
    _init_repo(tmp_path)
    adapter = StubAdapter(
        {
            "explorer": ok("found"),
            "domain_expert": ok("ok"),
            "architect": ok(_GOOD_PRE_TOURNAMENT_PLAN),
        }
    )

    cfg = default_config()
    cfg.tournaments.plan.enabled = True
    cfg.tournaments.plan.num_branches = 1
    cfg.tournaments.impl.enabled = False
    registry = build_registry(cfg)
    orch = Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-tournament-gate-parse",
    )

    async def fake_run_plan_tournament(
        orch, plan_md, intent, spec_hash, **_k
    ):
        return "this is not parseable markdown at all"

    from orchestrator import plan_phase as pp

    monkeypatch.setattr(pp, "run_plan_tournament", fake_run_plan_tournament)

    plan = await orch.plan("Add subtract")
    assert plan is not None

    from state.ledger import read_entries

    ledger = read_entries(tmp_path)
    rejections = [
        e for e in ledger if e.op == "tournament_output_rejected_structurally"
    ]
    assert len(rejections) == 1
    assert rejections[0].payload["reason"] == "parse_error"


@pytest.mark.asyncio
async def test_tournament_refined_clean_plan_passes_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean tournament refinement passes the gate cleanly: no
    rejection op, the refined plan title is the one persisted."""
    _init_repo(tmp_path)
    adapter = StubAdapter(
        {
            "explorer": ok("found"),
            "domain_expert": ok("ok"),
            "architect": ok(_GOOD_PRE_TOURNAMENT_PLAN),
        }
    )

    cfg = default_config()
    cfg.tournaments.plan.enabled = True
    cfg.tournaments.plan.num_branches = 1
    cfg.tournaments.impl.enabled = False
    registry = build_registry(cfg)
    orch = Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-tournament-gate-clean",
    )

    refined = _GOOD_PRE_TOURNAMENT_PLAN.replace(
        "# Plan: Add subtract", "# Plan: Add subtract REFINED"
    )

    async def fake_run_plan_tournament(
        orch, plan_md, intent, spec_hash, **_k
    ):
        return refined

    from orchestrator import plan_phase as pp

    monkeypatch.setattr(pp, "run_plan_tournament", fake_run_plan_tournament)

    plan = await orch.plan("Add subtract")
    assert plan is not None
    assert plan.metadata["title"] == "Add subtract REFINED"

    from state.ledger import read_entries

    ledger = read_entries(tmp_path)
    rejections = [
        e for e in ledger if e.op == "tournament_output_rejected_structurally"
    ]
    assert rejections == []
