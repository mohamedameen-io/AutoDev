"""Tests for :mod:`src.orchestrator.plan_phase`."""

from __future__ import annotations

from pathlib import Path

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator.plan_phase import (
    PlanParseError,
    parse_plan_markdown,
)
from state.schemas import Plan

from stub_adapter import StubAdapter, ok


CANONICAL_PLAN_MD = """
# Plan: Add subtract(a, b)

## Phase 1: Implement

### Task 1.1: Add subtract function to math.py
  - Description: Add subtract(a, b) that returns a - b
  - Files: math.py
  - Acceptance:
    - [ ] Function subtract defined
    - [ ] Returns correct value for positive ints

### Task 1.2: Add pytest test
  - Description: Verify subtract with 3 positive cases
  - Files: test_math.py
  - Acceptance:
    - [ ] pytest passes

## Phase 2: Document

### Task 2.1: Update README
  - Description: mention subtract
  - Files: README.md
  - Acceptance:
    - [ ] README mentions subtract
"""


def test_parse_plan_markdown_canonical() -> None:
    plan = parse_plan_markdown(CANONICAL_PLAN_MD, spec_hash="deadbeef")
    assert isinstance(plan, Plan)
    assert plan.spec_hash == "deadbeef"
    assert plan.metadata["title"] == "Add subtract(a, b)"
    assert len(plan.phases) == 2
    p1, p2 = plan.phases
    assert p1.id == "1"
    assert p1.title == "Implement"
    assert len(p1.tasks) == 2
    t11 = p1.tasks[0]
    assert t11.id == "1.1"
    assert t11.title.startswith("Add subtract")
    assert t11.files == ["math.py"]
    assert len(t11.acceptance) == 2
    assert t11.acceptance[0].description.startswith("Function subtract")
    assert p2.id == "2"
    assert p2.tasks[0].id == "2.1"


def test_parse_plan_markdown_missing_title_raises() -> None:
    with pytest.raises(PlanParseError):
        parse_plan_markdown("## Phase 1: x\n### Task 1.1: y\n")


def test_parse_plan_markdown_missing_phases_raises() -> None:
    with pytest.raises(PlanParseError):
        parse_plan_markdown("# Plan: nothing\n")


def test_parse_plan_markdown_phase_without_tasks_raises() -> None:
    with pytest.raises(PlanParseError):
        parse_plan_markdown("# Plan: x\n## Phase 1: empty\n## Phase 2: also empty\n")


def test_parse_plan_markdown_without_description_uses_title() -> None:
    md = """
# Plan: minimal

## Phase 1: x

### Task 1.1: do the thing
"""
    plan = parse_plan_markdown(md)
    t = plan.phases[0].tasks[0]
    assert t.description == "do the thing"
    assert t.files == []
    assert t.acceptance == []


# --- COMPLEXITY: capture tests ---------------------------------------------


CANONICAL_PLAN_MD_WITH_COMPLEXITY = f"{CANONICAL_PLAN_MD}\nCOMPLEXITY: medium\n"


def test_parse_plan_markdown_captures_complexity_medium() -> None:
    plan = parse_plan_markdown(CANONICAL_PLAN_MD_WITH_COMPLEXITY)
    assert plan.complexity == "medium"


def test_parse_plan_markdown_captures_complexity_simple() -> None:
    md = f"{CANONICAL_PLAN_MD}\nCOMPLEXITY: simple\n"
    plan = parse_plan_markdown(md)
    assert plan.complexity == "simple"


def test_parse_plan_markdown_captures_complexity_complex() -> None:
    md = f"{CANONICAL_PLAN_MD}\nCOMPLEXITY: complex\n"
    plan = parse_plan_markdown(md)
    assert plan.complexity == "complex"


def test_parse_plan_markdown_no_complexity_line_returns_none() -> None:
    """Legacy plans without a COMPLEXITY: line gracefully default to None."""
    plan = parse_plan_markdown(CANONICAL_PLAN_MD)
    assert plan.complexity is None


def test_parse_plan_markdown_complexity_case_insensitive() -> None:
    """COMPLEXITY: header and value match case-insensitively but normalize to lowercase."""
    md_upper = f"{CANONICAL_PLAN_MD}\nCOMPLEXITY: MEDIUM\n"
    plan_upper = parse_plan_markdown(md_upper)
    assert plan_upper.complexity == "medium"

    md_lower = f"{CANONICAL_PLAN_MD}\ncomplexity: simple\n"
    plan_lower = parse_plan_markdown(md_lower)
    assert plan_lower.complexity == "simple"


def test_parse_plan_markdown_complexity_invalid_value_treated_as_missing() -> None:
    """Unknown bucket like ``foo`` doesn't match the regex; complexity stays None."""
    md = f"{CANONICAL_PLAN_MD}\nCOMPLEXITY: foo\n"
    plan = parse_plan_markdown(md)
    assert plan.complexity is None


def test_parse_plan_markdown_strips_complexity_from_body() -> None:
    """The COMPLEXITY: line must not leak into any phase/task title or description."""
    plan = parse_plan_markdown(CANONICAL_PLAN_MD_WITH_COMPLEXITY)
    for phase in plan.phases:
        assert "COMPLEXITY:" not in phase.title
        assert "COMPLEXITY:" not in phase.description
        for task in phase.tasks:
            assert "COMPLEXITY:" not in task.title
            assert "COMPLEXITY:" not in task.description


def test_parse_plan_markdown_complexity_at_end_of_file_with_trailing_newline() -> None:
    """COMPLEXITY: as the last line — surrounded by newlines — parses cleanly."""
    md = f"{CANONICAL_PLAN_MD}\n\nCOMPLEXITY: complex\n"
    plan = parse_plan_markdown(md)
    assert plan.complexity == "complex"
    # Body should still parse normally.
    assert len(plan.phases) == 2


# --- Full-flow tests using StubAdapter -------------------------------------


def _make_orch(cwd: Path, adapter: StubAdapter) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    registry = build_registry(cfg)
    return Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-test-plan",
    )


@pytest.mark.asyncio
async def test_plan_phase_happy_path(tmp_path: Path) -> None:
    adapter = StubAdapter(
        {
            "explorer": ok("codebase has math.py and a pytest test"),
            "domain_expert": ok("no unusual domain considerations"),
            "architect": ok(CANONICAL_PLAN_MD),
        }
    )
    orch = _make_orch(tmp_path, adapter)
    plan = await orch.plan("Add subtract(a, b)")
    assert isinstance(plan, Plan)
    assert len(plan.phases) == 2
    assert adapter.count("explorer") == 1
    assert adapter.count("domain_expert") == 1
    assert adapter.count("architect") == 1
    evdir = tmp_path / ".autodev" / "evidence"
    assert (evdir / "plan-explore-explore.json").exists()
    assert (evdir / "plan-domain_expert-domain_expert.json").exists()


@pytest.mark.asyncio
async def test_plan_phase_parse_retry_on_bad_architect_output(
    tmp_path: Path,
) -> None:
    """First architect call returns malformed markdown; retry succeeds."""
    bad_then_good = [
        ok("NO HEADING AT ALL"),
        ok(CANONICAL_PLAN_MD),
    ]
    adapter = StubAdapter(
        {
            "explorer": ok("found stuff"),
            "domain_expert": ok("ok"),
            "architect": bad_then_good,
        }
    )
    orch = _make_orch(tmp_path, adapter)
    plan = await orch.plan("Add subtract")
    assert plan is not None
    assert adapter.count("architect") == 2


@pytest.mark.asyncio
async def test_plan_phase_persists_via_ledger(tmp_path: Path) -> None:
    adapter = StubAdapter(
        {
            "explorer": ok("ok"),
            "domain_expert": ok("ok"),
            "architect": ok(CANONICAL_PLAN_MD),
        }
    )
    orch = _make_orch(tmp_path, adapter)
    await orch.plan("Add subtract")
    from state.plan_manager import PlanManager

    pm = PlanManager(tmp_path, session_id="reader")
    plan = await pm.load()
    assert plan is not None
    assert len(plan.phases) == 2
    assert (tmp_path / ".autodev" / "plan.json").exists()
    assert (tmp_path / ".autodev" / "plan-ledger.jsonl").exists()
    assert (tmp_path / ".autodev" / "spec.md").exists()
    spec_text = (tmp_path / ".autodev" / "spec.md").read_text()
    assert "subtract" in spec_text


# --- Tournament-failure salvage tests (v0.6.0 / Issue 2) -------------------


SALVAGE_INCUMBENT_3_MD = """# Plan: Salvaged at pass 3

## Phase 1: Implement

### Task 1.1: pass-3 task
  - Description: from incumbent_after_03.md
  - Files: foo.py
  - Acceptance:
    - [ ] pass-3 marker present
"""


SALVAGE_INCUMBENT_5_MD = """# Plan: Salvaged at pass 5

## Phase 1: Implement

### Task 1.1: pass-5 task
  - Description: from incumbent_after_05.md
  - Files: foo.py
  - Acceptance:
    - [ ] pass-5 marker present
"""


@pytest.mark.asyncio
async def test_plan_phase_falls_back_to_latest_incumbent_on_tournament_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``run_plan_tournament`` raises ``TournamentError``, ``run_plan_phase``
    must read the latest ``incumbent_after_NN.md`` from disk rather than dropping
    refinement and falling back to the original architect output.
    """
    from errors import TournamentError
    from orchestrator import plan_phase as plan_phase_mod
    from orchestrator.plan_tournament_runner import _plan_tournament_id

    adapter = StubAdapter(
        {
            "explorer": ok("found stuff"),
            "domain_expert": ok("ok"),
            "architect": ok(CANONICAL_PLAN_MD),
        }
    )
    cfg = default_config()
    cfg.tournaments.plan.enabled = True
    cfg.tournaments.plan.num_branches = 1  # legacy single-branch path
    cfg.tournaments.impl.enabled = False
    registry = build_registry(cfg)
    orch = Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-test-salvage",
    )

    # Pre-populate the on-disk tournament dir with two incumbents.
    spec_hash = plan_phase_mod._spec_hash("Add subtract(a, b)")
    tournament_id = _plan_tournament_id(spec_hash)
    artifact_dir = tmp_path / ".autodev" / "tournaments" / tournament_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "incumbent_after_03.md").write_text(
        SALVAGE_INCUMBENT_3_MD, encoding="utf-8"
    )
    (artifact_dir / "incumbent_after_05.md").write_text(
        SALVAGE_INCUMBENT_5_MD, encoding="utf-8"
    )

    async def _raise(*args: object, **kwargs: object) -> str:
        raise TournamentError("simulated tournament failure")

    monkeypatch.setattr(plan_phase_mod, "run_plan_tournament", _raise)

    plan = await orch.plan("Add subtract(a, b)")
    assert plan is not None
    # The recovered plan came from incumbent_after_05.md (highest pass).
    assert plan.metadata["title"] == "Salvaged at pass 5"
    # And not from incumbent_after_03.md.
    assert plan.metadata["title"] != "Salvaged at pass 3"
    # And not from the original architect output (CANONICAL_PLAN_MD title).
    assert plan.metadata["title"] != "Add subtract(a, b)"


@pytest.mark.asyncio
async def test_plan_phase_falls_back_to_original_when_no_incumbents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No on-disk incumbents → legacy behavior: fall back to architect's plan."""
    from errors import TournamentError
    from orchestrator import plan_phase as plan_phase_mod

    adapter = StubAdapter(
        {
            "explorer": ok("found stuff"),
            "domain_expert": ok("ok"),
            "architect": ok(CANONICAL_PLAN_MD),
        }
    )
    cfg = default_config()
    cfg.tournaments.plan.enabled = True
    cfg.tournaments.plan.num_branches = 1  # legacy single-branch path
    cfg.tournaments.impl.enabled = False
    registry = build_registry(cfg)
    orch = Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-test-salvage-no-incumbent",
    )

    async def _raise(*args: object, **kwargs: object) -> str:
        raise TournamentError("simulated tournament failure")

    monkeypatch.setattr(plan_phase_mod, "run_plan_tournament", _raise)

    plan = await orch.plan("Add subtract(a, b)")
    assert plan is not None
    # The legacy path: architect's CANONICAL_PLAN_MD title.
    assert plan.metadata["title"] == "Add subtract(a, b)"


# ---------------------------------------------------------------------------
# v0.12.0 — plan_phase dispatch on cfg.tournaments.plan.num_branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_phase_dispatches_to_multi_branch_when_num_branches_gt_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``cfg.tournaments.plan.num_branches > 1`` → dispatch to
    :func:`run_multi_branch_plan_tournament`. Legacy
    :func:`run_plan_tournament` is NOT called on this path."""
    from orchestrator import plan_phase as plan_phase_mod

    multi_called = {"count": 0, "n_branches": None}
    single_called = {"count": 0}

    async def fake_multi(
        orch: object,
        plan_md: str,
        intent: str,
        spec_hash: str,
        *,
        n_branches: int,
    ) -> object:
        multi_called["count"] += 1
        multi_called["n_branches"] = n_branches
        # Return a synthetic outcome.
        from orchestrator.multi_branch_tournament import (
            BranchOutcome,
            MultiBranchOutcome,
        )

        return MultiBranchOutcome(
            branches=[
                BranchOutcome(
                    branch_index=i,
                    success=True,
                    final_md=CANONICAL_PLAN_MD,
                    error=None,
                )
                for i in range(n_branches)
            ],
            final_md=CANONICAL_PLAN_MD,
            meta_history=[],
        )

    async def fake_single(*args: object, **kwargs: object) -> str:
        single_called["count"] += 1
        return CANONICAL_PLAN_MD

    monkeypatch.setattr(
        plan_phase_mod, "run_multi_branch_plan_tournament", fake_multi
    )
    monkeypatch.setattr(plan_phase_mod, "run_plan_tournament", fake_single)

    adapter = StubAdapter(
        {
            "explorer": ok("ok"),
            "domain_expert": ok("ok"),
            "architect": ok(CANONICAL_PLAN_MD),
        }
    )
    cfg = default_config()
    cfg.tournaments.plan.enabled = True
    cfg.tournaments.plan.num_branches = 3  # multi-branch path
    cfg.tournaments.impl.enabled = False
    registry = build_registry(cfg)
    orch = Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-test-multi-dispatch",
    )

    plan = await orch.plan("Add subtract(a, b)")
    assert plan is not None
    assert multi_called["count"] == 1
    assert multi_called["n_branches"] == 3
    # Legacy single-branch path was NOT taken.
    assert single_called["count"] == 0


@pytest.mark.asyncio
async def test_plan_phase_retries_on_missing_file_and_includes_hint(
    tmp_path: Path,
) -> None:
    """v0.24.3: when the architect emits a plan with files that don't exist
    on disk, the parse-retry envelope fires and the second-pass envelope's
    ``context["hint"]`` carries the missing-file paragraph (with the ``[new]``
    opt-out instructions and a "do not exist on disk" marker).

    The first architect response references ``imaginary.cpp`` which won't be
    found; the second references ``math.py`` (which we create + commit) so
    the retry succeeds.
    """
    import subprocess

    # Bootstrap a populated git repo so ``_RepoFileSnapshot`` has tracked
    # files to validate against. A non-git or empty-git tree no-ops the
    # validator and the retry path is never exercised.
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=str(tmp_path), check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=str(tmp_path), check=True
    )
    (tmp_path / "math.py").write_text("def add(a, b): return a + b\n")
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
    subprocess.run(
        ["git", "commit", "-qm", "init"], cwd=str(tmp_path), check=True
    )

    bad_plan_md = """
# Plan: Add subtract

## Phase 1: Implement

### Task 1.1: bogus path
  - Description: references a path that does not exist
  - Files: imaginary.cpp
  - Acceptance:
    - [ ] something
"""
    good_plan_md = """
# Plan: Add subtract

## Phase 1: Implement

### Task 1.1: real path
  - Description: refs a real file
  - Files: math.py
  - Acceptance:
    - [ ] passes
"""
    bad_then_good = [ok(bad_plan_md), ok(good_plan_md)]
    adapter = StubAdapter(
        {
            "explorer": ok("found stuff"),
            "domain_expert": ok("ok"),
            "architect": bad_then_good,
        }
    )
    orch = _make_orch(tmp_path, adapter)

    plan = await orch.plan("Add subtract")
    assert plan is not None
    assert adapter.count("architect") == 2

    # The second architect call's prompt must carry the missing-file hint
    # paragraph with "do not exist on disk" + the [new] opt-out instructions.
    architect_prompts = adapter.prompts_for("architect")
    assert len(architect_prompts) == 2
    second_prompt = architect_prompts[1]
    assert "do not exist on disk" in second_prompt
    assert "[new]" in second_prompt


@pytest.mark.asyncio
async def test_plan_phase_dispatches_to_single_branch_when_num_branches_eq_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``cfg.tournaments.plan.num_branches == 1`` → legacy single-branch
    path; multi-branch entry-point NOT invoked. Regression for v0.11.x
    callers who haven't enabled fan-out."""
    from orchestrator import plan_phase as plan_phase_mod

    multi_called = {"count": 0}
    single_called = {"count": 0}

    async def fake_multi(*args: object, **kwargs: object) -> object:
        multi_called["count"] += 1
        raise AssertionError("multi-branch path should not be invoked when num_branches=1")

    async def fake_single(*args: object, **kwargs: object) -> str:
        single_called["count"] += 1
        return CANONICAL_PLAN_MD

    monkeypatch.setattr(
        plan_phase_mod, "run_multi_branch_plan_tournament", fake_multi
    )
    monkeypatch.setattr(plan_phase_mod, "run_plan_tournament", fake_single)

    adapter = StubAdapter(
        {
            "explorer": ok("ok"),
            "domain_expert": ok("ok"),
            "architect": ok(CANONICAL_PLAN_MD),
        }
    )
    cfg = default_config()
    cfg.tournaments.plan.enabled = True
    cfg.tournaments.plan.num_branches = 1  # single-branch path
    cfg.tournaments.impl.enabled = False
    registry = build_registry(cfg)
    orch = Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-test-single-dispatch",
    )

    plan = await orch.plan("Add subtract(a, b)")
    assert plan is not None
    assert single_called["count"] == 1
    assert multi_called["count"] == 0
