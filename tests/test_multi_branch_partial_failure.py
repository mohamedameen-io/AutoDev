"""v0.12.0 multi-branch partial-failure tests.

Validates the survivor-floor enforcement in
:func:`run_multi_branch_plan_tournament`:

- Floor for N=3 is ``max(2, ceil(3/2)) = 2``.
- 2 of 3 succeed → meta-merges 2 survivors (above floor).
- 1 of 3 succeeds → raises TournamentError (below floor).
- 0 of 3 succeed → raises TournamentError (well below floor).
- 3 of 3 succeed → meta-merges all 3.

Strategy: monkeypatch ``run_plan_tournament`` to raise on selected
branch indices. ``asyncio.gather(..., return_exceptions=True)`` is the
load-bearing detail — a single branch's failure must NOT cancel its
siblings (each failure is captured into BranchOutcome.error).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agents import build_registry
from config.defaults import default_config
from errors import TournamentError
from orchestrator import Orchestrator
from orchestrator import multi_branch_tournament as mbt

from stub_adapter import StubAdapter, ok


_SPEC_HASH = "0123456789abcdef"


def _make_orch(tmp_path: Path) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.num_judges = 3
    cfg.tournaments.auto_disable_for_models = []
    registry = build_registry(cfg)
    return Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=StubAdapter({"explorer": ok("ok")}),
        registry=registry,
        session_id="sess-test-partial",
    )


def _patch_run_plan_tournament(
    monkeypatch: pytest.MonkeyPatch,
    fail_indices: set[int],
) -> list[int]:
    """Patch run_plan_tournament to fail on indices in ``fail_indices``.

    Returns the recorded list of branch indices invoked. Used to verify
    that all branches were attempted (not cancelled by sibling failure).
    """
    invoked: list[int] = []

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
        invoked.append(branch_index if branch_index is not None else -1)
        if branch_index in fail_indices:
            raise RuntimeError(f"branch {branch_index} synthetic failure")
        return f"# Plan: branch-{branch_index}\n"

    monkeypatch.setattr(mbt, "run_plan_tournament", fake_run)

    # Also stub the meta-merge so we don't try to invoke real LLM calls.
    async def fake_meta(
        orch: Any, candidates: list[str], spec: str, spec_hash: str
    ) -> tuple[str, list]:
        return candidates[0], []

    monkeypatch.setattr(mbt, "_meta_merge_pairwise", fake_meta)
    return invoked


# ---------------------------------------------------------------------------
# All branches succeed: meta-merges all N
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_succeed_meta_merges_three(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3 of 3 branches succeed → meta-merge sees 3 candidates."""
    invoked = _patch_run_plan_tournament(monkeypatch, fail_indices=set())

    captured_candidates: list[list[str]] = []

    async def capturing_meta(
        orch: Any, candidates: list[str], spec: str, spec_hash: str
    ) -> tuple[str, list]:
        captured_candidates.append(list(candidates))
        return "# Plan: merged\n", []

    monkeypatch.setattr(mbt, "_meta_merge_pairwise", capturing_meta)

    orch = _make_orch(tmp_path)
    outcome = await mbt.run_multi_branch_plan_tournament(
        orch,
        initial_md="# Plan: draft\n",
        spec="spec",
        spec_hash=_SPEC_HASH,
        n_branches=3,
    )

    assert sorted(invoked) == [0, 1, 2]
    assert len(captured_candidates) == 1
    assert len(captured_candidates[0]) == 3
    assert outcome.final_md == "# Plan: merged\n"
    assert all(b.success for b in outcome.branches)


# ---------------------------------------------------------------------------
# 2 of 3 succeed: above floor → meta-merges 2 survivors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_of_three_succeed_meta_merges_two_survivors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2 of 3 branches succeed → still above floor (2) → meta-merge proceeds
    with the 2 survivors. The failed branch is recorded but excluded from
    the meta-merge inputs."""
    invoked = _patch_run_plan_tournament(monkeypatch, fail_indices={1})

    captured_candidates: list[list[str]] = []

    async def capturing_meta(
        orch: Any, candidates: list[str], spec: str, spec_hash: str
    ) -> tuple[str, list]:
        captured_candidates.append(list(candidates))
        return "# Plan: merged\n", []

    monkeypatch.setattr(mbt, "_meta_merge_pairwise", capturing_meta)

    orch = _make_orch(tmp_path)
    outcome = await mbt.run_multi_branch_plan_tournament(
        orch,
        initial_md="# Plan: draft\n",
        spec="spec",
        spec_hash=_SPEC_HASH,
        n_branches=3,
    )

    # All 3 branches were attempted (sibling failure did NOT cancel others).
    assert sorted(invoked) == [0, 1, 2]
    # Meta-merge invoked with exactly 2 survivor candidates.
    assert len(captured_candidates) == 1
    assert len(captured_candidates[0]) == 2
    # Survivors are branch 0 and branch 2 (by index ordering preserved).
    assert "branch-0" in captured_candidates[0][0]
    assert "branch-2" in captured_candidates[0][1]
    # BranchOutcome list reflects the failure.
    failed = [b for b in outcome.branches if not b.success]
    assert len(failed) == 1
    assert failed[0].branch_index == 1
    assert failed[0].error is not None
    assert "synthetic failure" in failed[0].error


# ---------------------------------------------------------------------------
# 1 of 3 succeeds: BELOW floor → TournamentError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_of_three_succeeds_raises_tournament_error_below_floor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1 of 3 succeeds → below the survivor floor (2) → TournamentError.

    The plan_phase fallback path then walks per-branch incumbent_after_NN.md
    files to salvage what was on disk (commit 10).
    """
    _patch_run_plan_tournament(monkeypatch, fail_indices={1, 2})

    orch = _make_orch(tmp_path)
    with pytest.raises(TournamentError, match="survivor floor"):
        await mbt.run_multi_branch_plan_tournament(
            orch,
            initial_md="# Plan: draft\n",
            spec="spec",
            spec_hash=_SPEC_HASH,
            n_branches=3,
        )


# ---------------------------------------------------------------------------
# 0 of 3 succeed: zero-survivor → TournamentError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zero_succeed_raises_tournament_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0 of 3 succeed → TournamentError, all branches recorded as failed."""
    _patch_run_plan_tournament(monkeypatch, fail_indices={0, 1, 2})

    orch = _make_orch(tmp_path)
    with pytest.raises(TournamentError, match="0 of 3"):
        await mbt.run_multi_branch_plan_tournament(
            orch,
            initial_md="# Plan: draft\n",
            spec="spec",
            spec_hash=_SPEC_HASH,
            n_branches=3,
        )


# ---------------------------------------------------------------------------
# Sibling-cancellation regression: verify gather(return_exceptions=True)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failing_branch_does_not_cancel_siblings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``asyncio.gather(..., return_exceptions=True)`` ensures one branch's
    exception does NOT cancel the sibling tasks.

    Strategy: branch 1 fails IMMEDIATELY at step 0 of its execution;
    branches 0 and 2 ``await asyncio.sleep(0.05)`` then return success.
    Without ``return_exceptions=True``, gather would propagate branch 1's
    exception and cancel siblings before they finished. With it,
    siblings complete and we record 2 successes + 1 failure.
    """
    import asyncio

    invoked: list[int] = []
    completed: list[int] = []

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
        invoked.append(branch_index if branch_index is not None else -1)
        if branch_index == 1:
            raise RuntimeError("immediate failure on branch 1")
        await asyncio.sleep(0.05)
        completed.append(branch_index if branch_index is not None else -1)
        return f"# Plan: branch-{branch_index}\n"

    monkeypatch.setattr(mbt, "run_plan_tournament", fake_run)

    async def fake_meta(
        orch: Any, candidates: list[str], spec: str, spec_hash: str
    ) -> tuple[str, list]:
        return "# Plan: merged\n", []

    monkeypatch.setattr(mbt, "_meta_merge_pairwise", fake_meta)

    orch = _make_orch(tmp_path)
    outcome = await mbt.run_multi_branch_plan_tournament(
        orch,
        initial_md="x",
        spec="spec",
        spec_hash=_SPEC_HASH,
        n_branches=3,
    )
    # Both surviving branches completed despite branch 1's instant failure.
    assert sorted(completed) == [0, 2]
    assert len([b for b in outcome.branches if b.success]) == 2
    assert len([b for b in outcome.branches if not b.success]) == 1
