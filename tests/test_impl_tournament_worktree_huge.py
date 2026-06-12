"""v0.40.0 (huge-repo Gap 3): impl-tournament worktree path is huge-safe.

The impl-tournament worktree creation is a SEPARATE code path from the
execute-phase ``create_per_task`` fixed in 00162a2. The tournament engine
(:class:`tournament.ImplTournament`) calls ``WorktreeManager.create(nonce,
base_ref="HEAD")`` with no scope; previously the runner built the manager
with no ``huge_mode`` → 60 s ``git worktree add`` timeout + full checkout,
which timed out on the Unity LFS repo and left a stale ``.git/index.lock``.

These tests assert the runner now:
  (a) builds the tournament ``WorktreeManager`` with ``huge_mode`` + the
      huge create-timeout on a huge repo, and threads a default sparse
      cone from the task's files; and
  (b) ``WorktreeManager.create`` applies ``default_sparse_paths`` when the
      caller passes no explicit ``sparse_paths`` (the engine's call shape),
      passing the huge timeout to ``git worktree add``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import worktree as wt_mod
from orchestrator.impl_tournament_runner import (
    _resolve_wm_huge_mode,
    _task_sparse_cone,
)
from orchestrator.worktree import WorktreeManager


# ── Runner-level helpers: huge-mode resolution + task cone ──────────────


def _orch(*, is_huge: bool, mode: str = "auto", sparse_flag: bool = False):
    """Minimal duck-typed orchestrator for the runner helpers."""
    cfg = SimpleNamespace(
        worktree_huge_repo_mode=mode,
        worktree_huge_create_timeout_s=600,
        worktree_sparse_checkout_enabled=sparse_flag,
    )
    return SimpleNamespace(
        cfg=cfg,
        _repo_capacity=SimpleNamespace(is_huge=is_huge),
    )


def test_resolve_wm_huge_mode_auto_keys_off_capacity() -> None:
    assert _resolve_wm_huge_mode(_orch(is_huge=True)) is True
    assert _resolve_wm_huge_mode(_orch(is_huge=False)) is False


def test_resolve_wm_huge_mode_on_off_overrides() -> None:
    assert _resolve_wm_huge_mode(_orch(is_huge=False, mode="on")) is True
    assert _resolve_wm_huge_mode(_orch(is_huge=True, mode="off")) is False


def test_task_sparse_cone_unions_files_and_extended_scope() -> None:
    task = SimpleNamespace(
        files=["src/a.py", "src/b.py"],
        extended_scope=["src/b.py", "include/c.h"],
    )
    # Deduped, order-preserving union.
    assert _task_sparse_cone(task) == ["src/a.py", "src/b.py", "include/c.h"]


def test_task_sparse_cone_empty_is_none() -> None:
    task = SimpleNamespace(files=[], extended_scope=[])
    assert _task_sparse_cone(task) is None


# ── WorktreeManager.create honors default_sparse_paths ──────────────────


def _capture_git(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Patch ``_run_git`` to record the worktree-add timeout + sparse set."""
    seen: dict = {}

    async def _fake_run_git(cwd, args, stdin=None, timeout_s=60.0):
        args = list(args)
        if args[:1] == ["--version"]:
            return (0, "git version 2.40.1", "")
        if args[:2] == ["worktree", "add"]:
            seen["add_timeout"] = timeout_s
            seen["add_args"] = args
        if args[:2] == ["sparse-checkout", "set"]:
            seen["sparse_set"] = args[2:]
        return (0, "", "")

    monkeypatch.setattr(wt_mod, "_run_git", _fake_run_git)
    monkeypatch.setattr(
        wt_mod.worktree_state, "record_create", lambda *a, **k: None
    )
    return seen


@pytest.mark.asyncio
async def test_create_uses_default_sparse_cone_and_huge_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine calls ``create(nonce, base_ref="HEAD")`` with no scope.
    With a huge-mode manager carrying a default cone, the create must go
    sparse (``--no-checkout`` + ``sparse-checkout set <cone>``) AND pass the
    huge timeout to ``git worktree add``."""
    seen = _capture_git(monkeypatch)
    mgr = WorktreeManager(
        main_repo=tmp_path,
        tournament_dir=tmp_path / "t",
        huge_mode=True,
        huge_create_timeout_s=600.0,
        default_sparse_paths=["src/foo/bar.py", "src/foo/baz.py"],
    )
    # Engine's exact call shape: label + base_ref, NO sparse_paths.
    await mgr.create("b-1234567", base_ref="HEAD")

    # Sparse path was taken (--no-checkout) with the huge timeout.
    assert "--no-checkout" in seen["add_args"]
    assert seen["add_timeout"] == 600.0
    # The default cone was applied verbatim.
    assert seen["sparse_set"] == ["src/foo/bar.py", "src/foo/baz.py"]


@pytest.mark.asyncio
async def test_create_explicit_sparse_overrides_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit ``sparse_paths`` arg wins over the instance default."""
    seen = _capture_git(monkeypatch)
    mgr = WorktreeManager(
        main_repo=tmp_path,
        tournament_dir=tmp_path / "t",
        huge_mode=True,
        default_sparse_paths=["src/default.py"],
    )
    await mgr.create("b-1", base_ref="HEAD", sparse_paths=["src/explicit.py"])
    assert seen["sparse_set"] == ["src/explicit.py"]


@pytest.mark.asyncio
async def test_create_no_default_no_explicit_is_full_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No default cone + no explicit scope → legacy full checkout (no
    ``--no-checkout`` / ``sparse-checkout set``). Preserves small-repo and
    opt-out behavior."""
    seen = _capture_git(monkeypatch)
    mgr = WorktreeManager(
        main_repo=tmp_path,
        tournament_dir=tmp_path / "t",
        huge_mode=False,
    )
    await mgr.create("b-1", base_ref="HEAD")
    assert "--no-checkout" not in seen["add_args"]
    assert "sparse_set" not in seen
    # Small repo → default 60s timeout preserved.
    assert seen["add_timeout"] == 60.0


# ── End-to-end: run_impl_tournament builds a huge-safe manager ──────────


@pytest.mark.asyncio
async def test_runner_builds_huge_safe_worktree_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run_impl_tournament`` on a huge repo constructs the tournament
    ``WorktreeManager`` with huge_mode + huge timeout + the task cone.

    Strategy: intercept ``WorktreeManager`` in the runner namespace with a
    capturing stub and ``ImplTournament`` with a no-op, then run the runner
    against a huge-flagged orchestrator and assert the constructor kwargs.
    """
    import datetime as _dt
    import subprocess

    from agents import build_registry
    from config.defaults import default_config
    from orchestrator import Orchestrator
    from orchestrator import impl_tournament_runner as itr
    from state.schemas import Phase, Plan, Task
    from tournament import ImplBundle
    from stub_adapter import StubAdapter

    # Minimal git repo (runner writes evidence + reads HEAD).
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "T"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path, check=True, capture_output=True,
    )

    cfg = default_config()
    cfg.tournaments.impl.enabled = True
    cfg.tournaments.impl.num_judges = 1
    cfg.tournaments.impl.max_rounds = 1
    cfg.tournaments.plan.enabled = False
    cfg.agents["judge"].model = "sonnet"
    cfg.tournaments.auto_disable_for_models = []
    registry = build_registry(cfg)
    orch = Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=StubAdapter({}),
        registry=registry,
        session_id="sess-huge-wt",
    )
    # Flag the repo huge (auto mode keys off this).
    orch._repo_capacity = SimpleNamespace(is_huge=True)

    def _iso() -> str:
        return _dt.datetime.now(_dt.timezone.utc).isoformat()

    plan = Plan(
        plan_id="p", spec_hash="h",
        phases=[Phase(id="1", title="W", tasks=[
            Task(
                id="1.1", phase_id="1", title="t", description="d",
                files=["src/foo.py"], extended_scope=["include/foo.h"],
            )
        ])],
        created_at=_iso(), updated_at=_iso(),
    )
    await orch.plan_manager.init_plan(plan)
    task = await orch.plan_manager.get_task("1.1")
    assert task is not None

    captured: dict = {}

    class _CapturingWM:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        async def cleanup_all(self) -> None:
            return None

    class _NoopTournament:
        def __init__(self, **_kwargs) -> None:
            pass

        async def run(self, *, task_prompt, initial):
            return initial, []

    monkeypatch.setattr(itr, "WorktreeManager", _CapturingWM)
    monkeypatch.setattr(itr, "ImplTournament", _NoopTournament)

    initial = ImplBundle(task_id="1.1", task_description="d", diff="")
    await itr.run_impl_tournament(orch, task, initial)

    assert captured.get("huge_mode") is True
    assert captured.get("huge_create_timeout_s") == 600.0
    assert captured.get("default_sparse_paths") == ["src/foo.py", "include/foo.h"]
