"""v0.22.1 A3 regression: WorktreeManager huge_mode timeout extension.

On Unity-scale repos (358K files, 3 GB) ``git worktree add`` can take
80-180 seconds for a full checkout; the historical 60 s timeout killed
it. v0.22.1 adds a ``huge_mode`` flag that extends the per-call
``_run_git`` timeout to ``huge_create_timeout_s`` (default 600 s).
Sparse-by-default lands in v0.23.0 C1.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator import worktree as wt_mod
from orchestrator.worktree import WorktreeManager


def test_worktree_default_timeout_is_60s(tmp_path: Path) -> None:
    """Without huge_mode, _create_timeout_s returns the historical 60s."""
    mgr = WorktreeManager(main_repo=tmp_path, tournament_dir=tmp_path / "t")
    assert mgr._create_timeout_s() == 60.0
    assert mgr._huge_mode is False


def test_worktree_huge_mode_uses_extended_timeout(tmp_path: Path) -> None:
    """huge_mode=True bumps the timeout to 600s by default."""
    mgr = WorktreeManager(
        main_repo=tmp_path,
        tournament_dir=tmp_path / "t",
        huge_mode=True,
    )
    assert mgr._create_timeout_s() == 600.0
    assert mgr._huge_mode is True


def test_worktree_huge_mode_custom_timeout(tmp_path: Path) -> None:
    """huge_create_timeout_s param is respected."""
    mgr = WorktreeManager(
        main_repo=tmp_path,
        tournament_dir=tmp_path / "t",
        huge_mode=True,
        huge_create_timeout_s=900.0,
    )
    assert mgr._create_timeout_s() == 900.0


def test_worktree_huge_mode_off_ignores_huge_timeout(tmp_path: Path) -> None:
    """huge_create_timeout_s is inert when huge_mode=False."""
    mgr = WorktreeManager(
        main_repo=tmp_path,
        tournament_dir=tmp_path / "t",
        huge_mode=False,
        huge_create_timeout_s=900.0,
    )
    assert mgr._create_timeout_s() == 60.0


def _capture_git_timeouts(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    """Patch ``_run_git`` to record the ``timeout_s`` of the worktree-add call.

    Returns a dict that, after the create call, holds ``{"add": <timeout>}``
    for the ``worktree add`` invocation. All git calls succeed (rc=0).
    """
    seen: dict[str, float] = {}

    async def _fake_run_git(cwd, args, stdin=None, timeout_s=60.0):
        args = list(args)
        if args[:2] == ["worktree", "add"]:
            seen["add"] = timeout_s
        if args[:1] == ["--version"]:
            return (0, "git version 2.40.1", "")
        return (0, "", "")

    monkeypatch.setattr(wt_mod, "_run_git", _fake_run_git)
    # Skip the worktree-state manifest write (filesystem side effect).
    monkeypatch.setattr(
        wt_mod.worktree_state, "record_create", lambda *a, **k: None
    )
    return seen


@pytest.mark.asyncio
async def test_create_per_task_nonsparse_uses_huge_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gap-1 regression: the non-sparse create_per_task path passes the
    huge timeout (was the 60s _run_git default before the fix)."""
    seen = _capture_git_timeouts(monkeypatch)
    mgr = WorktreeManager(
        main_repo=tmp_path,
        tournament_dir=tmp_path / "t",
        huge_mode=True,
        huge_create_timeout_s=600.0,
    )
    await mgr.create_per_task("1.1", sparse_paths=None)
    assert seen["add"] == 600.0


@pytest.mark.asyncio
async def test_create_per_task_sparse_uses_huge_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gap-1 regression: the sparse --no-checkout create_per_task path
    also passes the huge timeout on huge repos."""
    seen = _capture_git_timeouts(monkeypatch)
    # Sibling-header expansion shells out to subprocess; stub it to empty.
    monkeypatch.setattr(
        wt_mod, "_sibling_header_paths", lambda *a, **k: set()
    )
    mgr = WorktreeManager(
        main_repo=tmp_path,
        tournament_dir=tmp_path / "t",
        huge_mode=True,
        huge_create_timeout_s=600.0,
    )
    await mgr.create_per_task("1.1", sparse_paths=["src/foo/bar.py"])
    assert seen["add"] == 600.0


@pytest.mark.asyncio
async def test_create_per_task_nonhuge_uses_default_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Small repo (huge_mode off) → create_per_task keeps the 60s default."""
    seen = _capture_git_timeouts(monkeypatch)
    mgr = WorktreeManager(
        main_repo=tmp_path,
        tournament_dir=tmp_path / "t",
        huge_mode=False,
    )
    await mgr.create_per_task("1.1", sparse_paths=None)
    assert seen["add"] == 60.0
