"""v0.21.0 A2 — multi-branch impl-tournament fan-out tests.

Covers :func:`orchestrator.impl_tournament_runner.run_multi_branch_impl_tournament`:

* 3-branch fan-out invokes ``run_impl_tournament`` 3 times in parallel,
* survivor floor enforcement,
* diff-synthesis meta-merge happy path,
* synth fallback to strongest survivor on parse failure,
* survivor-floor below threshold raises TournamentError.

Strategy: monkeypatch ``run_impl_tournament`` and the meta-merge inner
calls so tests don't need a live LLM client / git worktree.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agents import build_registry
from config.defaults import default_config
from errors import TournamentError
from orchestrator import Orchestrator
from orchestrator import impl_tournament_runner as itr
from state.schemas import Phase, Plan, Task
from tournament.impl_tournament import ImplBundle, ImplContentHandler

from stub_adapter import StubAdapter, ok


def _make_orch(tmp_path: Path) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = True
    cfg.tournaments.impl.num_judges = 1
    cfg.tournaments.impl.convergence_k = 1
    cfg.tournaments.impl.max_rounds = 1
    cfg.tournaments.auto_disable_for_models = []
    registry = build_registry(cfg)
    adapter = StubAdapter({"synthesizer": ok("```diff\ndiff --git a/x b/x\n@@\n+merged\n```")})
    return Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-multi-impl",
    )


def _make_task(tid: str = "1.1") -> Task:
    return Task(
        id=tid,
        phase_id="1",
        title="t",
        description="implement x",
    )


def _make_initial_bundle(tid: str = "1.1") -> ImplBundle:
    return ImplBundle(
        task_id=tid,
        task_description="implement x",
        diff="",
        files_changed=[],
        variant_label="A",
    )


async def _init_plan(orch: Orchestrator) -> None:
    """Create a minimal plan for the orchestrator's plan_manager."""
    import datetime as _dt

    plan = Plan(
        plan_id="p-multi-impl",
        spec_hash="abc",
        phases=[Phase(id="1", title="p", tasks=[_make_task("1.1")])],
        created_at=_dt.datetime.now(_dt.timezone.utc).isoformat(),
        updated_at=_dt.datetime.now(_dt.timezone.utc).isoformat(),
    )
    await orch.plan_manager.init_plan(plan)


# ---------------------------------------------------------------------------
# Survivor floor unit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "n,expected",
    [(1, 2), (2, 2), (3, 2), (4, 2), (5, 3)],
)
def test_impl_survivor_floor(n: int, expected: int) -> None:
    """``_impl_survivor_floor(N)`` returns ``max(2, ceil(N/2))``."""
    assert itr._impl_survivor_floor(n) == expected


# ---------------------------------------------------------------------------
# Fan-out + meta-merge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_three_branch_fanout_invokes_three_impl_tournaments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3 branches → 3 parallel ``run_impl_tournament`` calls."""
    orch = _make_orch(tmp_path)
    await _init_plan(orch)
    task = _make_task()
    initial = _make_initial_bundle()

    invocations: list[Any] = []

    async def fake_run_impl(
        orch_arg: Any,
        task_arg: Task,
        initial_arg: ImplBundle,
        *,
        branch_config: Any = None,
    ) -> ImplBundle:
        invocations.append(branch_config)
        return ImplBundle(
            task_id=task_arg.id,
            task_description=task_arg.description,
            diff=f"diff --git a/x b/x\n+branch-{len(invocations)}\n",
            variant_label="A",
        )

    monkeypatch.setattr(itr, "run_impl_tournament", fake_run_impl)

    # Bypass meta-merge worktree by stubbing the inner helper.
    async def fake_meta_merge(
        orch_arg: Any,
        task_arg: Task,
        initial_arg: ImplBundle,
        diffs: list[str],
    ) -> ImplBundle:
        return ImplBundle(
            task_id=task_arg.id,
            task_description=task_arg.description,
            diff="merged-result",
            variant_label="AB",
        )

    monkeypatch.setattr(
        itr, "_impl_meta_merge_via_diff_synthesis", fake_meta_merge
    )

    out = await itr.run_multi_branch_impl_tournament(
        orch, task, initial, n_branches=3
    )
    assert out.diff == "merged-result"
    assert len(invocations) == 3


@pytest.mark.asyncio
async def test_survivor_floor_failure_raises_tournament_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When fewer than ``max(2, ceil(N/2))`` branches succeed, raise."""
    orch = _make_orch(tmp_path)
    await _init_plan(orch)
    task = _make_task()
    initial = _make_initial_bundle()

    async def fake_run_impl(
        orch_arg: Any, task_arg: Task, initial_arg: ImplBundle, *, branch_config: Any = None
    ) -> ImplBundle:
        # All branches fail — only 0 survivors, floor=2 → raise.
        raise RuntimeError("simulated branch failure")

    monkeypatch.setattr(itr, "run_impl_tournament", fake_run_impl)

    with pytest.raises(TournamentError):
        await itr.run_multi_branch_impl_tournament(
            orch, task, initial, n_branches=3
        )


@pytest.mark.asyncio
async def test_n1_short_circuit_uses_single_branch_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``n_branches=1`` short-circuits to ``run_impl_tournament``."""
    orch = _make_orch(tmp_path)
    await _init_plan(orch)
    task = _make_task()
    initial = _make_initial_bundle()

    invocations: list[int] = []

    async def fake_run_impl(
        orch_arg: Any, task_arg: Task, initial_arg: ImplBundle, *, branch_config: Any = None
    ) -> ImplBundle:
        invocations.append(1)
        return ImplBundle(
            task_id=task_arg.id,
            task_description=task_arg.description,
            diff="single-branch",
            variant_label="A",
        )

    monkeypatch.setattr(itr, "run_impl_tournament", fake_run_impl)

    out = await itr.run_multi_branch_impl_tournament(
        orch, task, initial, n_branches=1
    )
    assert out.diff == "single-branch"
    assert len(invocations) == 1


# ---------------------------------------------------------------------------
# Diff extraction unit
# ---------------------------------------------------------------------------


def test_extract_diff_block_fenced() -> None:
    """``_extract_diff_block`` extracts a fenced ```diff ... ``` body."""
    text = "Some preamble.\n```diff\ndiff --git a/x b/x\n+hello\n```\nTrailing."
    out = itr._extract_diff_block(text)
    assert "diff --git a/x b/x" in out
    assert "+hello" in out


def test_extract_diff_block_generic_fenced_with_diff_marker() -> None:
    """Generic fenced block containing ``diff --git`` is extracted."""
    text = "Pre.\n```\ndiff --git a/x b/x\n+y\n```"
    out = itr._extract_diff_block(text)
    assert "diff --git a/x b/x" in out


def test_extract_diff_block_bare_diff_git_prefix() -> None:
    """Bare ``diff --git`` prefix is extracted (no fence)."""
    text = "preamble\ndiff --git a/x b/x\n+content\n"
    out = itr._extract_diff_block(text)
    assert out.startswith("diff --git a/x b/x")


def test_extract_diff_block_no_diff_returns_empty() -> None:
    """No diff-shaped content → empty string (caller falls back)."""
    assert itr._extract_diff_block("just prose, no diff") == ""
    assert itr._extract_diff_block("") == ""


# ---------------------------------------------------------------------------
# Fallback to strongest survivor
# ---------------------------------------------------------------------------


def test_fallback_strongest_survivor_picks_largest_diff() -> None:
    """``_fallback_strongest_survivor`` returns the longest diff as winner."""
    initial = _make_initial_bundle()
    diffs = ["short", "this-is-a-much-longer-diff-with-more-content", "mid"]
    out = itr._fallback_strongest_survivor(initial, diffs)
    assert out.diff == "this-is-a-much-longer-diff-with-more-content"
    assert out.variant_label == "AB"
    assert out.notes == "meta-merge-fallback"


def test_fallback_strongest_survivor_empty_diffs_returns_initial() -> None:
    """No diffs → return initial_bundle unchanged."""
    initial = _make_initial_bundle()
    out = itr._fallback_strongest_survivor(initial, [])
    assert out is initial


# ---------------------------------------------------------------------------
# render_for_diff_synthesis
# ---------------------------------------------------------------------------


def test_render_for_diff_synthesis_includes_all_diffs() -> None:
    """Each diff appears as ``CANDIDATE N`` block."""
    h = ImplContentHandler()
    out = h.render_for_diff_synthesis(
        task_prompt="implement x",
        diffs=[
            "diff --git a/x b/x\n+a\n",
            "diff --git a/y b/y\n+b\n",
            "diff --git a/z b/z\n+c\n",
        ],
    )
    assert "CANDIDATE 1" in out
    assert "CANDIDATE 2" in out
    assert "CANDIDATE 3" in out
    assert "implement x" in out
    assert "diff --git a/x b/x" in out


def test_render_for_diff_synthesis_empty_diff_marker() -> None:
    """Empty diff slot is rendered as ``<empty diff>``."""
    h = ImplContentHandler()
    out = h.render_for_diff_synthesis(
        task_prompt="implement x",
        diffs=["", "diff --git a/y b/y\n+b\n"],
    )
    assert "<empty diff>" in out


def test_render_for_diff_synthesis_zero_diffs_raises() -> None:
    """0 diffs is caller error → ValueError."""
    h = ImplContentHandler()
    with pytest.raises(ValueError):
        h.render_for_diff_synthesis(task_prompt="x", diffs=[])
