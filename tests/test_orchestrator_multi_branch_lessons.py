"""v0.15.0: lessons emission from multi-branch tournament meta-merge.

After the meta-merge produces a single final markdown from N survivor
branches, the dispatcher must emit:
* ONE ``winner_promoted`` event for the final survivor (family =
  ``"multi-branch-meta-merge"``).
* ONE ``discard`` event for each *failed* branch (a branch whose
  per-branch tournament raised). These are the candidates that didn't
  even make it to the meta-merge step.

The successful per-branch tournaments already emit their own per-pass
discards / winners via :mod:`orchestrator.plan_tournament_runner` (see
:mod:`tests.test_orchestrator_plan_tournament_lessons`); this module
focuses on the meta-merge boundary specifically.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator import multi_branch_tournament as mbt

from stub_adapter import StubAdapter, ok


_SPEC_HASH = "0123456789abcdef"


def _make_orch(tmp_path: Path) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = True
    cfg.tournaments.plan.num_judges = 3
    cfg.tournaments.plan.convergence_k = 1
    cfg.tournaments.plan.max_rounds = 1
    cfg.tournaments.auto_disable_for_models = []
    cfg.tournaments.impl.enabled = False
    cfg.hive.enabled = False
    registry = build_registry(cfg)
    adapter = StubAdapter({"explorer": ok("ok")})
    return Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-test-multi-branch-lessons",
    )


@pytest.mark.asyncio
async def test_meta_merge_emits_winner_promoted_for_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After meta-merge produces ``final_md``, a ``winner_promoted`` event
    keyed to the ``multi-branch-meta-merge`` family must be persisted.
    """
    async def fake_run(
        orch: Any,
        initial_md: str,
        spec: str,
        spec_hash: str,
        *,
        branch_index: int | None = None,
        branch_seed: int | None = None,
        **_extra: Any,
    ) -> str:
        return f"# Plan: branch-{branch_index}\n"

    monkeypatch.setattr(mbt, "run_plan_tournament", fake_run)

    async def fake_meta(
        orch: Any, candidates: list[str], spec: str, spec_hash: str
    ) -> tuple[str, list]:
        return "# Plan: meta-merged\n", []

    monkeypatch.setattr(mbt, "_meta_merge_pairwise", fake_meta)

    orch = _make_orch(tmp_path)
    await mbt.run_multi_branch_plan_tournament(
        orch,
        initial_md="# Plan: draft\n",
        spec="spec",
        spec_hash=_SPEC_HASH,
        n_branches=3,
    )

    entries = await orch.knowledge.read_all(tier="swarm")
    families = [e.metadata.get("family") for e in entries]
    event_types = [e.metadata.get("event_type") for e in entries]
    assert "multi-branch-meta-merge" in families
    # At least one winner_promoted event from the meta-merge boundary.
    matching = [
        e
        for e in entries
        if e.metadata.get("family") == "multi-branch-meta-merge"
        and e.metadata.get("event_type") == "winner_promoted"
    ]
    assert matching, f"no winner_promoted at meta-merge; got types={event_types}"


@pytest.mark.asyncio
async def test_meta_merge_emits_discard_for_failed_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a branch fails (raises) and the survivor floor is still met,
    the dispatcher must emit a ``discard`` lesson for that failed branch
    so future runs can avoid recreating the same failure mode.
    """
    async def fake_run(
        orch: Any,
        initial_md: str,
        spec: str,
        spec_hash: str,
        *,
        branch_index: int | None = None,
        branch_seed: int | None = None,
        **_extra: Any,
    ) -> str:
        if branch_index == 1:
            raise RuntimeError("simulated branch 1 failure")
        return f"# Plan: branch-{branch_index}\n"

    monkeypatch.setattr(mbt, "run_plan_tournament", fake_run)

    async def fake_meta(
        orch: Any, candidates: list[str], spec: str, spec_hash: str
    ) -> tuple[str, list]:
        return "# Plan: meta-merged\n", []

    monkeypatch.setattr(mbt, "_meta_merge_pairwise", fake_meta)

    orch = _make_orch(tmp_path)
    await mbt.run_multi_branch_plan_tournament(
        orch,
        initial_md="# Plan: draft\n",
        spec="spec",
        spec_hash=_SPEC_HASH,
        n_branches=3,
    )

    entries = await orch.knowledge.read_all(tier="swarm")
    matching = [
        e
        for e in entries
        if e.metadata.get("family") == "multi-branch-meta-merge"
        and e.metadata.get("event_type") == "discard"
    ]
    assert matching, "expected a discard lesson for the failed branch"
    # Hypothesis text should reference the failed branch's index.
    bodies = " | ".join(e.text for e in matching)
    assert "branch=1" in bodies or "branch_index=1" in bodies or "branch 1" in bodies


@pytest.mark.asyncio
async def test_meta_merge_lesson_failure_does_not_break_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If knowledge.record_tournament_event raises during meta-merge
    finalization, run_multi_branch_plan_tournament must still return the
    meta-merged markdown."""
    async def fake_run(
        orch: Any,
        initial_md: str,
        spec: str,
        spec_hash: str,
        *,
        branch_index: int | None = None,
        branch_seed: int | None = None,
        **_extra: Any,
    ) -> str:
        return f"# Plan: branch-{branch_index}\n"

    monkeypatch.setattr(mbt, "run_plan_tournament", fake_run)

    async def fake_meta(
        orch: Any, candidates: list[str], spec: str, spec_hash: str
    ) -> tuple[str, list]:
        return "# Plan: meta-merged\n", []

    monkeypatch.setattr(mbt, "_meta_merge_pairwise", fake_meta)

    async def boom(self: Any, event: Any) -> None:
        raise RuntimeError("simulated knowledge failure")

    orch = _make_orch(tmp_path)
    monkeypatch.setattr(type(orch.knowledge), "record_tournament_event", boom)

    outcome = await mbt.run_multi_branch_plan_tournament(
        orch,
        initial_md="# Plan: draft\n",
        spec="spec",
        spec_hash=_SPEC_HASH,
        n_branches=3,
    )
    assert outcome.final_md == "# Plan: meta-merged\n"
