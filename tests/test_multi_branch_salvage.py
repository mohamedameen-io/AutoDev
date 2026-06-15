"""v0.12.0 multi-branch salvage tests.

Validates the v0.6.0 salvage path extension in
:func:`orchestrator.plan_phase.run_plan_phase` when a multi-branch
tournament raises :class:`TournamentError` (e.g. below survivor floor).

The salvage path:
1. ``run_multi_branch_plan_tournament`` raises TournamentError.
2. Plan-phase fallback walks ``tournaments/multi-{hash}/branch-N/``
   subdirs via :func:`latest_incumbent_md_across_branches`.
3. Returns the highest-pass-num ``incumbent_after_NN.md`` across all
   branches; ties broken by lowest branch_index.
4. If no on-disk incumbents, falls through to architect's plan markdown.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents import build_registry
from config.defaults import default_config
from errors import TournamentError
from orchestrator import Orchestrator
from orchestrator import plan_phase as plan_phase_mod

from stub_adapter import StubAdapter, ok


# A canonical plan markdown that parses cleanly into a Plan.
CANONICAL_PLAN_MD = """# Plan: Add subtract(a, b)

## Phase 1: Implement

### Task 1.1: Add subtract function to math.py
  - Description: Add subtract(a, b) that returns a - b
  - Files: math.py
  - Acceptance:
    - [ ] Function subtract defined
"""


def _salvage_md(label: str) -> str:
    """A parseable plan markdown with a recognizable title for assertions."""
    return f"""# Plan: {label}

## Phase 1: Salvaged

### Task 1.1: Salvaged work
  - Description: Recovered from on-disk incumbent
  - Files: salvaged.py
  - Acceptance:
    - [ ] recovered correctly
"""


def _make_orch(tmp_path: Path) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = True
    cfg.tournaments.plan.num_branches = 3  # multi-branch
    cfg.tournaments.impl.enabled = False
    cfg.tournaments.auto_disable_for_models = []
    # v0.41.0: these tests pre-seed incumbents under the raw-intent spec_hash.
    # Intake (on by default) would lock an enriched spec and rebind spec_hash,
    # moving the tournament dir away from the seeded one. Salvage is tested in
    # isolation here, so intake is scoped off (phase-presence shift, not a regression).
    cfg.intake.enabled = False
    registry = build_registry(cfg)
    adapter = StubAdapter(
        {
            "explorer": ok("ok"),
            "domain_expert": ok("ok"),
            "architect": ok(CANONICAL_PLAN_MD),
        }
    )
    return Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-test-multi-salvage",
    )


@pytest.mark.asyncio
async def test_multi_branch_salvage_picks_highest_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multi-branch tournament errors → salvage walks all branches and
    picks the highest-pass-num incumbent.

    Layout:
      branch-0/incumbent_after_03.md
      branch-1/incumbent_after_07.md  (winner)
      branch-2/incumbent_after_05.md
    """
    orch = _make_orch(tmp_path)

    # Pre-populate per-branch incumbents.
    spec_text = "Add subtract(a, b)"
    spec_hash = plan_phase_mod._spec_hash(spec_text)
    parent = (
        tmp_path / ".autodev" / "tournaments" / f"multi-{spec_hash[:8]}"
    )
    for idx, pass_num, label in [
        (0, 3, "branch-0 pass 3"),
        (1, 7, "branch-1 pass 7"),
        (2, 5, "branch-2 pass 5"),
    ]:
        d = parent / f"branch-{idx}"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"incumbent_after_{pass_num:02d}.md").write_text(
            _salvage_md(label), encoding="utf-8"
        )

    # Stub the multi-branch tournament to raise.
    async def fake_multi(*args: object, **kwargs: object) -> object:
        raise TournamentError("under floor")

    monkeypatch.setattr(
        plan_phase_mod, "run_multi_branch_plan_tournament", fake_multi
    )

    plan = await orch.plan(spec_text)
    assert plan is not None
    # The salvaged plan should have come from branch-1 pass 7.
    assert plan.metadata["title"] == "branch-1 pass 7"


@pytest.mark.asyncio
async def test_multi_branch_salvage_tiebreak_lowest_branch_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When two branches have the same top pass num, lowest branch_index wins."""
    orch = _make_orch(tmp_path)
    spec_text = "Add subtract(a, b)"
    spec_hash = plan_phase_mod._spec_hash(spec_text)
    parent = (
        tmp_path / ".autodev" / "tournaments" / f"multi-{spec_hash[:8]}"
    )
    for idx, label in [
        (0, "branch-0 pass 4"),
        (1, "branch-1 pass 4"),
        (2, "branch-2 pass 4"),
    ]:
        d = parent / f"branch-{idx}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "incumbent_after_04.md").write_text(
            _salvage_md(label), encoding="utf-8"
        )

    async def fake_multi(*args: object, **kwargs: object) -> object:
        raise TournamentError("under floor")

    monkeypatch.setattr(
        plan_phase_mod, "run_multi_branch_plan_tournament", fake_multi
    )

    plan = await orch.plan(spec_text)
    assert plan is not None
    # Tie-break: lowest branch_index wins.
    assert plan.metadata["title"] == "branch-0 pass 4"


@pytest.mark.asyncio
async def test_multi_branch_salvage_falls_back_to_architect_when_no_incumbents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No per-branch incumbents on disk → fall back to the architect's plan.

    This is the legacy v0.6.0 fall-through behavior preserved across the
    multi-branch extension: when there's nothing to salvage, downstream
    parsing uses the original plan markdown.
    """
    orch = _make_orch(tmp_path)

    async def fake_multi(*args: object, **kwargs: object) -> object:
        raise TournamentError("nothing on disk")

    monkeypatch.setattr(
        plan_phase_mod, "run_multi_branch_plan_tournament", fake_multi
    )

    plan = await orch.plan("Add subtract(a, b)")
    assert plan is not None
    # No salvage available → legacy path: architect's CANONICAL_PLAN_MD.
    assert plan.metadata["title"] == "Add subtract(a, b)"


@pytest.mark.asyncio
async def test_multi_branch_salvage_only_one_branch_has_incumbents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One branch has incumbents, others empty → salvage from the populated one.

    Common scenario: 2 branches crash early, 1 makes progress; the
    progress is salvaged correctly.
    """
    orch = _make_orch(tmp_path)
    spec_text = "Add subtract(a, b)"
    spec_hash = plan_phase_mod._spec_hash(spec_text)
    parent = (
        tmp_path / ".autodev" / "tournaments" / f"multi-{spec_hash[:8]}"
    )
    # Only branch 2 has any incumbents.
    d = parent / "branch-2"
    d.mkdir(parents=True, exist_ok=True)
    (d / "incumbent_after_06.md").write_text(
        _salvage_md("branch-2 pass 6"), encoding="utf-8"
    )
    # Branches 0 and 1 exist but empty.
    (parent / "branch-0").mkdir(parents=True, exist_ok=True)
    (parent / "branch-1").mkdir(parents=True, exist_ok=True)

    async def fake_multi(*args: object, **kwargs: object) -> object:
        raise TournamentError("under floor")

    monkeypatch.setattr(
        plan_phase_mod, "run_multi_branch_plan_tournament", fake_multi
    )

    plan = await orch.plan(spec_text)
    assert plan is not None
    assert plan.metadata["title"] == "branch-2 pass 6"
