"""v0.22.1 A3 regression: WorktreeManager huge_mode timeout extension.

On Unity-scale repos (358K files, 3 GB) ``git worktree add`` can take
80-180 seconds for a full checkout; the historical 60 s timeout killed
it. v0.22.1 adds a ``huge_mode`` flag that extends the per-call
``_run_git`` timeout to ``huge_create_timeout_s`` (default 600 s).
Sparse-by-default lands in v0.23.0 C1.
"""

from __future__ import annotations

from pathlib import Path

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
