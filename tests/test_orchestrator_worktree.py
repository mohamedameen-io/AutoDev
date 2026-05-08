"""Tests for v0.11.0 additions to :class:`orchestrator.worktree.WorktreeManager`.

Covers:

* :meth:`create_per_task` — new convenience that places per-task
  worktrees under a ``tasks/`` subdirectory so they don't collide with
  impl-tournament label worktrees.
* :meth:`remove_per_task` — symmetric cleanup.
* :meth:`apply_patch_to_main` ``three_way=True`` flag — used by the
  conflict-escalation path.

Pre-existing impl-tournament tests live in
``test_impl_tournament_worktree.py`` and are not duplicated here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orchestrator.worktree import WorktreeError, WorktreeManager


def _init_git_repo(path: Path) -> None:
    """Initialize a minimal git repo with one commit."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    (path / "README.md").write_text("# test\n")
    subprocess.run(["git", "add", "."], cwd=str(path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )


@pytest.mark.asyncio
async def test_create_per_task_uses_tasks_subdir(tmp_path: Path) -> None:
    """create_per_task('1.1') places the worktree at ``<dir>/tasks/1.1``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    wt_dir = tmp_path / "execute_worktrees"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)

    wt = await mgr.create_per_task("1.1")
    assert wt == wt_dir / "tasks" / "1.1"
    assert wt.exists()
    assert wt.is_dir()
    # The README should be present (HEAD checked out into the worktree).
    assert (wt / "README.md").exists()

    await mgr.cleanup_all()


@pytest.mark.asyncio
async def test_create_per_task_duplicate_raises(tmp_path: Path) -> None:
    """Double-create on the same task id raises WorktreeError."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    wt_dir = tmp_path / "execute_worktrees"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)

    await mgr.create_per_task("1.1")
    with pytest.raises(WorktreeError, match="already exists"):
        await mgr.create_per_task("1.1")

    await mgr.cleanup_all()


@pytest.mark.asyncio
async def test_remove_per_task_removes_worktree(tmp_path: Path) -> None:
    """remove_per_task tears down the worktree even with uncommitted edits."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    wt_dir = tmp_path / "execute_worktrees"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)

    wt = await mgr.create_per_task("2.5")
    # Dirty the worktree.
    (wt / "scratch.py").write_text("x = 1\n")
    assert wt.exists()

    await mgr.remove_per_task("2.5")
    assert not wt.exists()


@pytest.mark.asyncio
async def test_remove_per_task_nonexistent_is_noop(tmp_path: Path) -> None:
    """Removing a task id whose worktree was never created is a noop."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    wt_dir = tmp_path / "execute_worktrees"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)

    # Should not raise.
    await mgr.remove_per_task("ghost")


@pytest.mark.asyncio
async def test_create_per_task_coexists_with_impl_tournament_layout(
    tmp_path: Path,
) -> None:
    """Per-task and impl-variant worktrees can share the same tournament_dir."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    wt_dir = tmp_path / "tournament_dir"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)

    # Impl-style label worktrees at the top level.
    a = await mgr.create("a")
    b = await mgr.create("b")
    # Per-task worktree under tasks/.
    pt = await mgr.create_per_task("3.1")

    assert a == wt_dir / "a"
    assert b == wt_dir / "b"
    assert pt == wt_dir / "tasks" / "3.1"
    # All exist on disk.
    assert a.exists() and b.exists() and pt.exists()

    # cleanup_all sweeps everything.
    await mgr.cleanup_all()
    assert not a.exists()
    assert not b.exists()
    assert not pt.exists()


@pytest.mark.asyncio
async def test_apply_patch_three_way_succeeds_on_clean(tmp_path: Path) -> None:
    """apply_patch_to_main(three_way=True) still applies a clean diff cleanly."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    wt_dir = tmp_path / "execute_worktrees"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)

    wt = await mgr.create_per_task("1.1")
    (wt / "README.md").write_text("# updated by worker\n")

    await mgr.apply_patch_to_main(wt, three_way=True)
    assert (repo / "README.md").read_text() == "# updated by worker\n"

    await mgr.cleanup_all()


# ---------------------------------------------------------------------------
# v0.14.0 — apply_patch_to_main(edit_scope=...) hunk validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_patch_to_main_passes_when_scope_empty(tmp_path: Path) -> None:
    """``edit_scope=None`` (default) preserves legacy whole-repo behavior:
    patches anywhere in the worktree apply cleanly to main."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    wt_dir = tmp_path / "execute_worktrees"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)

    wt = await mgr.create_per_task("1.1")
    # Touch a file outside any "scope" — apply must succeed because no
    # scope is provided.
    (wt / "anywhere.py").write_text("x = 1\n")

    await mgr.apply_patch_to_main(wt, edit_scope=None)
    assert (repo / "anywhere.py").read_text() == "x = 1\n"

    await mgr.cleanup_all()


@pytest.mark.asyncio
async def test_apply_patch_to_main_passes_when_all_hunks_in_scope(
    tmp_path: Path,
) -> None:
    """``edit_scope=['src']`` with all diff hunks under ``src/`` applies."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    # Establish src/ tracked-file baseline so the diff has src/ paths.
    (repo / "src").mkdir()
    (repo / "src" / "foo.py").write_text("x = 0\n")
    subprocess.run(
        ["git", "add", "."], cwd=str(repo), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "src baseline"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )

    wt_dir = tmp_path / "execute_worktrees"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)
    wt = await mgr.create_per_task("1.1")
    (wt / "src" / "foo.py").write_text("x = 42\n")

    await mgr.apply_patch_to_main(wt, edit_scope=["src"])
    assert (repo / "src" / "foo.py").read_text() == "x = 42\n"

    await mgr.cleanup_all()


@pytest.mark.asyncio
async def test_apply_patch_to_main_rejects_out_of_scope_hunk_when_scope_set(
    tmp_path: Path,
) -> None:
    """A hunk targeting a path outside the configured scope aborts the
    apply with EditScopeViolation BEFORE any change lands on main."""
    from orchestrator.dag import EditScopeViolation

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    (repo / "src").mkdir()
    (repo / "src" / "foo.py").write_text("x = 0\n")
    subprocess.run(
        ["git", "add", "."], cwd=str(repo), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "src baseline"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )

    wt_dir = tmp_path / "execute_worktrees"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)
    wt = await mgr.create_per_task("1.1")
    # Two hunks: one in scope, one out.
    (wt / "src" / "foo.py").write_text("x = 42\n")
    (wt / "docs_out.md").write_text("forbidden write\n")

    with pytest.raises(EditScopeViolation):
        await mgr.apply_patch_to_main(wt, edit_scope=["src"])

    # main repo state is unchanged: src/foo.py still at baseline,
    # docs_out.md never created.
    assert (repo / "src" / "foo.py").read_text() == "x = 0\n"
    assert not (repo / "docs_out.md").exists()

    await mgr.cleanup_all()
