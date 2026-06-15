"""v0.41.0 (Workstream A3): tests for
:meth:`orchestrator.worktree.WorktreeManager.abort_failed_apply`.

Background: ``_apply_with_conflict_escalation`` (execute_phase.py) falls back
to ``apply_patch_to_main(three_way=True)`` on an initial apply conflict. When
the 3-way ALSO fails it used to mark the task blocked and return ``False``
*without cleaning the main working tree* — leaving ``<<<<<<<`` conflict
markers / an unmerged (``UU``) index behind. Those artifacts then bled into
the NEXT task's per-task worktree (created at ``HEAD`` of the main repo),
corrupting an otherwise-correct downstream diff.

``abort_failed_apply`` guarantees a clean tree (``git merge --abort`` if a
merge is in progress, else ``git reset --hard HEAD`` + scoped ``git clean``)
and is idempotent / safe to call when already clean.

These tests reproduce the exact ``git apply --3way`` failure mode where the
pre-flight ``--check`` passes (rc=0) but the real apply fails (rc=1) leaving
conflict markers, then assert the tree is clean afterwards and a subsequent
apply succeeds.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orchestrator.worktree import WorktreeError, WorktreeManager


def _git(cwd: Path, *args: str) -> str:
    """Run ``git <args>`` in ``cwd`` and return stdout (raises on failure)."""
    res = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )
    return res.stdout


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    _git(path, "config", "user.email", "test@test.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "commit.gpgsign", "false")
    (path / "f.txt").write_text("line1\nline2\nline3\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "init")


def _status(path: Path) -> str:
    return _git(path, "status", "--porcelain")


async def _induce_three_way_conflict(
    repo: Path, mgr: WorktreeManager
) -> Path:
    """Drive the manager into a left-dirty 3-way apply failure.

    Returns the per-task worktree path. After this call the main repo's
    working tree carries ``<<<<<<<`` conflict markers and an unmerged
    index entry for ``f.txt`` (mirrors the production failure the A3 fix
    cleans up).
    """
    # Worktree starts at the original HEAD (line2 baseline).
    wt = await mgr.create_per_task("1.1")
    # Worker edits line2 → WORKER_CHANGE in the worktree.
    (wt / "f.txt").write_text("line1\nWORKER_CHANGE\nline3\n")

    # Meanwhile main advances HEAD with a CONFLICTING change to line2.
    (repo / "f.txt").write_text("line1\nMAIN_CHANGE\nline3\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "main conflicting change")

    # The 3-way apply: --check passes but the real apply fails with
    # conflicts, leaving markers in the main tree → WorktreeError.
    with pytest.raises(WorktreeError):
        await mgr.apply_patch_to_main(wt, base_ref="HEAD", three_way=True)
    return wt


@pytest.mark.asyncio
async def test_three_way_failure_leaves_dirty_tree_then_abort_cleans(
    tmp_path: Path,
) -> None:
    """A failed 3-way apply leaves conflict markers; abort restores clean."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    mgr = WorktreeManager(
        main_repo=repo, tournament_dir=tmp_path / "execute_worktrees"
    )

    await _induce_three_way_conflict(repo, mgr)

    # Pre-condition: the tree is genuinely dirty (the bug this fix targets).
    assert _status(repo).strip() != "", "expected a dirty tree after 3-way fail"
    assert "<<<<<<<" in (repo / "f.txt").read_text()

    # The fix: abort_failed_apply scoped to the attempted target.
    await mgr.abort_failed_apply(targets=["f.txt"])

    # Post-condition: clean tree, no markers, file restored to HEAD.
    assert _status(repo).strip() == "", "tree must be clean after abort"
    contents = (repo / "f.txt").read_text()
    assert "<<<<<<<" not in contents
    assert ">>>>>>>" not in contents
    assert contents == "line1\nMAIN_CHANGE\nline3\n"

    await mgr.cleanup_all()


@pytest.mark.asyncio
async def test_subsequent_apply_succeeds_after_abort(tmp_path: Path) -> None:
    """After abort_failed_apply, a fresh per-task apply lands cleanly."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    mgr = WorktreeManager(
        main_repo=repo, tournament_dir=tmp_path / "execute_worktrees"
    )

    await _induce_three_way_conflict(repo, mgr)
    await mgr.abort_failed_apply(targets=["f.txt"])
    assert _status(repo).strip() == ""

    # A subsequent task touching a DIFFERENT file must apply cleanly — the
    # prior conflict residue is gone, so this stands in for "the next task's
    # diff is no longer corrupted".
    wt2 = await mgr.create_per_task("1.2")
    (wt2 / "new_file.py").write_text("x = 42\n")
    await mgr.apply_patch_to_main(wt2, base_ref="HEAD")

    assert (repo / "new_file.py").read_text() == "x = 42\n"

    await mgr.cleanup_all()


@pytest.mark.asyncio
async def test_abort_failed_apply_is_idempotent_on_clean_tree(
    tmp_path: Path,
) -> None:
    """Calling abort on an already-clean tree is a safe no-op (no raise)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    mgr = WorktreeManager(
        main_repo=repo, tournament_dir=tmp_path / "execute_worktrees"
    )

    before = _status(repo)
    # No conflict, no dirt — must not raise and must leave the tree as-is.
    await mgr.abort_failed_apply(targets=["f.txt"])
    await mgr.abort_failed_apply()  # also fine with no targets
    assert _status(repo) == before
    assert (repo / "f.txt").read_text() == "line1\nline2\nline3\n"


@pytest.mark.asyncio
async def test_abort_failed_apply_scopes_clean_to_targets(
    tmp_path: Path,
) -> None:
    """``git clean`` is scoped to ``targets`` — unrelated untracked files stay.

    The reset/clean must not sweep an untracked scratch file the operator
    (or an unrelated task) left elsewhere in the tree.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    mgr = WorktreeManager(
        main_repo=repo, tournament_dir=tmp_path / "execute_worktrees"
    )

    # An unrelated untracked file that must survive a scoped abort.
    (repo / "unrelated_scratch.txt").write_text("keep me\n")

    await _induce_three_way_conflict(repo, mgr)
    await mgr.abort_failed_apply(targets=["f.txt"])

    # The tracked conflict is cleaned, but the unrelated untracked file
    # outside the scope is preserved.
    assert (repo / "unrelated_scratch.txt").exists()
    assert (repo / "unrelated_scratch.txt").read_text() == "keep me\n"
    assert "<<<<<<<" not in (repo / "f.txt").read_text()

    await mgr.cleanup_all()
