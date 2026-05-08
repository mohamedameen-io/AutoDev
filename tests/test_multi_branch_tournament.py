"""v0.12.0 multi-branch plan-tournament orchestrator tests.

Covers :func:`run_multi_branch_plan_tournament` fan-out + meta-merge
plumbing. Branch failures → survivor floor enforcement is exercised in
``test_multi_branch_partial_failure.py``.

Strategy: monkeypatch ``run_plan_tournament`` in the multi-branch module
namespace to a recording stub so tests don't actually invoke a tournament.
The stub records its kwargs (``branch_index``, ``branch_seed``) and
returns a synthetic per-branch markdown so the meta-merge step has
realistic inputs.

For meta-merge tests, monkeypatch ``_run_meta_merge_step`` to a
recording stub so we don't need a live LLM client.
"""

from __future__ import annotations

import asyncio
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
    cfg.tournaments.plan.num_judges = 3  # small for fast tests
    cfg.tournaments.plan.convergence_k = 1
    cfg.tournaments.plan.max_rounds = 1
    cfg.tournaments.auto_disable_for_models = []
    cfg.tournaments.impl.enabled = False
    registry = build_registry(cfg)
    adapter = StubAdapter({"explorer": ok("ok")})
    return Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-test-multi-branch",
    )


# ---------------------------------------------------------------------------
# Survivor floor unit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "n,expected",
    [(1, 2), (2, 2), (3, 2), (4, 2), (5, 3)],
)
def test_survivor_floor_thresholds(n: int, expected: int) -> None:
    """``_survivor_floor(N)`` returns ``max(2, ceil(N/2))``.

    For N=3 (the v0.12.0 default), the floor is 2 — at most 1 branch may
    fail before the multi-branch runner raises TournamentError.
    """
    assert mbt._survivor_floor(n) == expected


# ---------------------------------------------------------------------------
# Fan-out: N parallel branches with divergent seeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_three_branches_default_invokes_three_run_plan_tournament(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default ``num_branches=3`` → three parallel ``run_plan_tournament``
    calls with branch_index ∈ {0, 1, 2} and divergent ``branch_seed`` per branch.

    Each ``branch_seed`` must equal ``int(spec_hash, 16) + branch_index``
    (the deterministic divergence rule documented in the v0.12.0 plan).
    """
    captured_calls: list[dict[str, Any]] = []

    async def fake_run(
        orch: Any,
        initial_md: str,
        spec: str,
        spec_hash: str,
        *,
        branch_index: int | None = None,
        branch_seed: int | None = None,
    ) -> str:
        captured_calls.append(
            {
                "branch_index": branch_index,
                "branch_seed": branch_seed,
                "spec_hash": spec_hash,
            }
        )
        return f"# Plan: branch-{branch_index}\n"

    # Monkeypatch run_plan_tournament in the mbt module's namespace AND
    # the helper that wraps it.
    monkeypatch.setattr(mbt, "run_plan_tournament", fake_run)

    # Bypass the meta-merge LLM calls.
    async def fake_meta(
        orch: Any, candidates: list[str], spec: str, spec_hash: str
    ) -> tuple[str, list]:
        return candidates[0], []

    monkeypatch.setattr(mbt, "_meta_merge_pairwise", fake_meta)

    orch = _make_orch(tmp_path)
    outcome = await mbt.run_multi_branch_plan_tournament(
        orch,
        initial_md="# Plan: draft\n",
        spec="user spec",
        spec_hash=_SPEC_HASH,
        n_branches=3,
    )

    assert len(captured_calls) == 3
    indices = sorted(c["branch_index"] for c in captured_calls)
    assert indices == [0, 1, 2]

    # Verify divergent seeds: int(spec_hash, 16) + branch_index.
    base = int(_SPEC_HASH, 16)
    seeds = sorted(c["branch_seed"] for c in captured_calls)
    expected_seeds = sorted([base + i for i in range(3)])
    assert seeds == expected_seeds
    # All seeds distinct.
    assert len(set(seeds)) == 3

    assert isinstance(outcome, mbt.MultiBranchOutcome)
    assert len(outcome.branches) == 3
    assert all(b.success for b in outcome.branches)


@pytest.mark.asyncio
async def test_branches_run_concurrently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All branches kick off concurrently (no serialization).

    Strategy: each fake branch sleeps 0.1s. With 3 branches running
    concurrently, total wall clock should be < 0.2s. With serialization,
    it would be ≥ 0.3s. Generous bound to avoid CI flakiness.
    """
    n = 3
    sleep_s = 0.1

    started_at: list[float] = []

    async def fake_run(
        orch: Any,
        initial_md: str,
        spec: str,
        spec_hash: str,
        *,
        branch_index: int | None = None,
        branch_seed: int | None = None,
    ) -> str:
        loop = asyncio.get_event_loop()
        started_at.append(loop.time())
        await asyncio.sleep(sleep_s)
        return f"# Plan: branch-{branch_index}\n"

    monkeypatch.setattr(mbt, "run_plan_tournament", fake_run)

    async def fake_meta(
        orch: Any, candidates: list[str], spec: str, spec_hash: str
    ) -> tuple[str, list]:
        return candidates[0], []

    monkeypatch.setattr(mbt, "_meta_merge_pairwise", fake_meta)

    orch = _make_orch(tmp_path)
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    await mbt.run_multi_branch_plan_tournament(
        orch,
        initial_md="# Plan: draft\n",
        spec="spec",
        spec_hash=_SPEC_HASH,
        n_branches=n,
    )
    elapsed = loop.time() - t0
    # Concurrency check: max start-time skew across branches < 0.05s
    # (they all kicked off near-simultaneously) AND total elapsed
    # < (n-1) * sleep_s (would be 0.2s if serialized).
    assert max(started_at) - min(started_at) < 0.05
    assert elapsed < (n * sleep_s) - 0.01


@pytest.mark.asyncio
async def test_n_branches_eq_1_produces_single_branch_passthrough(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``n_branches=1`` runs one branch and skips the meta-merge."""
    captured_calls: list[dict[str, Any]] = []

    async def fake_run(
        orch: Any,
        initial_md: str,
        spec: str,
        spec_hash: str,
        *,
        branch_index: int | None = None,
        branch_seed: int | None = None,
    ) -> str:
        captured_calls.append({"branch_index": branch_index})
        return "# Plan: only-branch\n"

    monkeypatch.setattr(mbt, "run_plan_tournament", fake_run)

    orch = _make_orch(tmp_path)
    outcome = await mbt.run_multi_branch_plan_tournament(
        orch,
        initial_md="# Plan: draft\n",
        spec="spec",
        spec_hash=_SPEC_HASH,
        n_branches=1,
    )
    assert len(captured_calls) == 1
    # n=1 hits the "single survivor" path → passthrough, no meta-merge.
    assert outcome.final_md == "# Plan: only-branch\n"
    assert outcome.meta_history == []


@pytest.mark.asyncio
async def test_n_branches_zero_rejected(tmp_path: Path) -> None:
    """``n_branches=0`` is caller misuse and raises ValueError."""
    orch = _make_orch(tmp_path)
    with pytest.raises(ValueError):
        await mbt.run_multi_branch_plan_tournament(
            orch,
            initial_md="x",
            spec="x",
            spec_hash=_SPEC_HASH,
            n_branches=0,
        )


# ---------------------------------------------------------------------------
# Pairwise meta-merge: order, count of steps, deterministic output
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_meta_merge_three_candidates_invokes_two_steps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3 candidates → 2 pairwise meta-merge steps (left-fold reduction)."""
    captured_steps: list[dict[str, Any]] = []

    async def fake_step(
        *,
        orch: Any,
        handler: Any,
        client: Any,
        spec: str,
        spec_hash: str,
        a_md: str,
        b_md: str,
        step_idx: int,
        num_judges: int,
        judge_model: str | None,
    ) -> tuple[str, Any]:
        captured_steps.append(
            {"step_idx": step_idx, "a_md": a_md, "b_md": b_md}
        )
        # Return a synthetic merged markdown for the next step to consume.
        merged = f"# Plan: merge({a_md.split(chr(10))[0]}, {b_md.split(chr(10))[0]})\n"
        # Synthetic PassResult for forensics.
        from tournament.core import PassResult

        result = PassResult(
            pass_num=1,
            winner="AB",
            scores={"A": 1, "B": 1, "AB": 2},
            valid_judges=1,
            elapsed_s=0.001,
            judge_details=[],
            incumbent_hash_before="aaaa",
            incumbent_hash_after="bbbb",
            meta={"meta_merge_step": step_idx},
        )
        return merged, result

    monkeypatch.setattr(mbt, "_run_meta_merge_step", fake_step)

    orch = _make_orch(tmp_path)
    candidates = [
        "# Plan: A\n",
        "# Plan: B\n",
        "# Plan: C\n",
    ]
    final_md, history = await mbt._meta_merge_pairwise(
        orch, candidates, spec="spec", spec_hash=_SPEC_HASH
    )

    # Step 0: synth(A, B) → m1
    # Step 1: synth(m1, C) → m2 (final)
    assert len(captured_steps) == 2
    assert captured_steps[0]["step_idx"] == 0
    assert captured_steps[0]["a_md"] == candidates[0]
    assert captured_steps[0]["b_md"] == candidates[1]
    assert captured_steps[1]["step_idx"] == 1
    # Step 1's a_md is the OUTPUT of step 0 (left-fold).
    assert captured_steps[1]["a_md"].startswith("# Plan: merge")
    assert captured_steps[1]["b_md"] == candidates[2]
    assert len(history) == 2


@pytest.mark.asyncio
async def test_meta_merge_two_candidates_invokes_one_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2 candidates → 1 pairwise step (synth(A, B))."""
    captured_steps: list[dict[str, Any]] = []

    async def fake_step(
        *,
        orch: Any,
        handler: Any,
        client: Any,
        spec: str,
        spec_hash: str,
        a_md: str,
        b_md: str,
        step_idx: int,
        num_judges: int,
        judge_model: str | None,
    ) -> tuple[str, Any]:
        captured_steps.append({"step_idx": step_idx})
        from tournament.core import PassResult

        return (
            "# Plan: merged\n",
            PassResult(
                pass_num=1,
                winner="AB",
                scores={"A": 0, "B": 0, "AB": 0},
                valid_judges=0,
                elapsed_s=0.0,
                judge_details=[],
                incumbent_hash_before="x",
                incumbent_hash_after="y",
                meta={},
            ),
        )

    monkeypatch.setattr(mbt, "_run_meta_merge_step", fake_step)

    orch = _make_orch(tmp_path)
    final_md, history = await mbt._meta_merge_pairwise(
        orch,
        ["# Plan: A\n", "# Plan: B\n"],
        spec="s",
        spec_hash=_SPEC_HASH,
    )
    assert len(captured_steps) == 1
    assert final_md == "# Plan: merged\n"
    assert len(history) == 1


@pytest.mark.asyncio
async def test_meta_merge_one_candidate_passes_through(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1 candidate → no meta-merge step, returns the survivor unchanged.

    This is the defensive 1-survivor edge case (floor=2 + only 1 success).
    Never expected in production because ``_survivor_floor() >= 2``, but
    kept correct so unit-level callers can pass single inputs.
    """
    called = []

    async def fake_step(*args: Any, **kwargs: Any) -> Any:
        called.append(args)
        raise AssertionError("should not be called for single survivor")

    monkeypatch.setattr(mbt, "_run_meta_merge_step", fake_step)

    orch = _make_orch(tmp_path)
    final_md, history = await mbt._meta_merge_pairwise(
        orch, ["# Plan: only\n"], spec="s", spec_hash=_SPEC_HASH
    )
    assert final_md == "# Plan: only\n"
    assert history == []
    assert called == []


@pytest.mark.asyncio
async def test_meta_merge_zero_candidates_raises(tmp_path: Path) -> None:
    """``len(candidates) == 0`` is caller misuse → ValueError."""
    orch = _make_orch(tmp_path)
    with pytest.raises(ValueError, match="no candidates"):
        await mbt._meta_merge_pairwise(
            orch, [], spec="s", spec_hash=_SPEC_HASH
        )


# ---------------------------------------------------------------------------
# Stable seed: deterministic across re-runs
# ---------------------------------------------------------------------------


def test_stable_seed_deterministic() -> None:
    """``_stable_seed`` returns identical output for identical inputs.

    Critical because Python's built-in :func:`hash` is randomized per
    process (PYTHONHASHSEED). The meta-merge depends on stable judge
    orders for resume to produce identical artifacts on re-run.
    """
    a = "# Plan: alpha\n"
    b = "# Plan: beta\n"
    s1 = mbt._stable_seed(a, b, "0")
    s2 = mbt._stable_seed(a, b, "0")
    assert s1 == s2


def test_stable_seed_differs_on_different_inputs() -> None:
    """Different inputs → different seeds (so different steps shuffle differently)."""
    s_a_b_0 = mbt._stable_seed("a", "b", "0")
    s_a_b_1 = mbt._stable_seed("a", "b", "1")
    s_a_c_0 = mbt._stable_seed("a", "c", "0")
    assert s_a_b_0 != s_a_b_1
    assert s_a_b_0 != s_a_c_0


# ---------------------------------------------------------------------------
# Artifact layout: tournaments/multi-{hash}/branch-N + meta-merge/
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_meta_merge_artifact_dir_under_multi_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_meta_merge_pairwise`` creates artifacts under
    ``tournaments/multi-{hash}/meta-merge/step-N/``."""

    async def fake_step(
        *,
        orch: Any,
        handler: Any,
        client: Any,
        spec: str,
        spec_hash: str,
        a_md: str,
        b_md: str,
        step_idx: int,
        num_judges: int,
        judge_model: str | None,
    ) -> tuple[str, Any]:
        from tournament.core import PassResult

        return (
            "merged\n",
            PassResult(
                pass_num=1,
                winner="A",
                scores={"A": 0, "B": 0, "AB": 0},
                valid_judges=0,
                elapsed_s=0.0,
                judge_details=[],
                incumbent_hash_before="x",
                incumbent_hash_after="y",
                meta={},
            ),
        )

    monkeypatch.setattr(mbt, "_run_meta_merge_step", fake_step)

    orch = _make_orch(tmp_path)
    await mbt._meta_merge_pairwise(
        orch,
        ["# A\n", "# B\n", "# C\n"],
        spec="s",
        spec_hash=_SPEC_HASH,
    )
    # Parent multi-merge dir was created (mkdir parents=True ok).
    expected_root = tmp_path / ".autodev" / "tournaments" / f"multi-{_SPEC_HASH[:8]}" / "meta-merge"
    assert expected_root.exists()


def test_multi_branch_parent_dir_helper(tmp_path: Path) -> None:
    """``multi_branch_parent_dir`` returns the expected layout root."""
    p = mbt.multi_branch_parent_dir(tmp_path, _SPEC_HASH)
    assert p == tmp_path / ".autodev" / "tournaments" / f"multi-{_SPEC_HASH[:8]}"
