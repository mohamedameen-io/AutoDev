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


# ---------------------------------------------------------------------------
# v0.25.1 Bug #1 — cleanup_all must not treat the per-task ``tasks/`` parent
# directory as a worktree label. Regression: cleanup_all iterdir()'d the
# tournament dir, found ``tasks/`` alongside impl labels ``a/``/``b/``, fed
# ``"tasks"`` through ``remove(label="tasks", force=True)`` and ultimately
# ``shutil.rmtree(<dir>/tasks)``, destroying every per-task worktree in one
# call along with any patches that hadn't yet been applied to main.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_rejects_reserved_tasks_label(tmp_path: Path) -> None:
    """``remove('tasks')`` must refuse — ``tasks`` is the parent container
    for per-task worktrees, not a worktree label. Letting it through
    caused ``cleanup_all`` to wipe all sibling per-task worktrees."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    wt_dir = tmp_path / "execute_worktrees"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)
    pt = await mgr.create_per_task("1.1")
    assert pt.exists()

    with pytest.raises(WorktreeError, match="reserved"):
        await mgr.remove("tasks", force=True)

    # The per-task worktree must still be intact.
    assert pt.exists()
    await mgr.cleanup_all()


@pytest.mark.asyncio
async def test_cleanup_all_does_not_force_remove_tasks_parent(
    tmp_path: Path,
) -> None:
    """``cleanup_all`` must not emit ``worktree.force_removed`` on the
    ``tasks/`` parent directory. The buggy implementation treated
    ``tasks/`` as a label and fell through to ``shutil.rmtree(tasks)``,
    destroying every in-flight per-task worktree."""
    import structlog

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    wt_dir = tmp_path / "execute_worktrees"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)

    pt_a = await mgr.create_per_task("2.1")
    pt_b = await mgr.create_per_task("2.3")
    # Simulate uncommitted work that should NOT be lost via force_remove.
    (pt_a / "scratch.txt").write_text("WIP 2.1\n")
    (pt_b / "scratch.txt").write_text("WIP 2.3\n")

    with structlog.testing.capture_logs() as cap:
        await mgr.cleanup_all()

    force_removed_on_parent = [
        ev
        for ev in cap
        if ev.get("event") == "worktree.force_removed"
        and ev.get("path", "").rstrip("/").endswith("/tasks")
    ]
    assert not force_removed_on_parent, (
        "cleanup_all force-removed the tasks parent directory; "
        f"events: {force_removed_on_parent}"
    )


# ---------------------------------------------------------------------------
# v0.25.1 Bug #2 — persistent integration (commit-per-task). Regression:
# ``apply_patch_to_main`` left the patch in the main repo's uncommitted
# working tree, so a downstream ``cleanup_all`` (Bug #1) or an operator
# ``git reset`` lost the work. Without a commit, the *next* per-task
# worktree created at HEAD also couldn't see prior tasks' changes, so
# cross-task dependencies cascaded into "coder adapter failure".
#
# Fix: optional ``commit_message`` parameter on ``apply_patch_to_main``.
# When supplied, the manager stages + commits the apply atomically with
# the supplied message. Subsequent ``create_per_task`` calls default to
# ``base_ref="HEAD"`` and therefore see the new commit. Empty / absent
# message preserves the legacy v0.25.0 behavior (impl-tournament uses
# the non-committing path).
# ---------------------------------------------------------------------------


def _head_sha(repo: Path) -> str:
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def _head_message(repo: Path) -> str:
    out = subprocess.run(
        ["git", "log", "-1", "--pretty=%B"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


@pytest.mark.asyncio
async def test_apply_patch_to_main_does_not_commit_without_message(
    tmp_path: Path,
) -> None:
    """Legacy behavior preserved: when ``commit_message`` is not
    supplied, ``apply_patch_to_main`` leaves the apply uncommitted in
    the working tree (impl-tournament path)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    head_before = _head_sha(repo)

    wt_dir = tmp_path / "execute_worktrees"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)
    wt = await mgr.create_per_task("1.1")
    (wt / "README.md").write_text("# changed by 1.1\n")

    await mgr.apply_patch_to_main(wt)
    assert (repo / "README.md").read_text() == "# changed by 1.1\n"
    assert _head_sha(repo) == head_before, "HEAD must not advance without a message"

    await mgr.cleanup_all()


@pytest.mark.asyncio
async def test_apply_patch_to_main_commits_when_message_given(
    tmp_path: Path,
) -> None:
    """v0.25.1 Bug #2: with ``commit_message`` set, the apply lands on
    main as a new commit. Uses ``git add -A`` + ``git commit`` so the
    work is durable across any subsequent ``cleanup_all`` or operator
    reset."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    head_before = _head_sha(repo)

    wt_dir = tmp_path / "execute_worktrees"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)
    wt = await mgr.create_per_task("1.1")
    (wt / "README.md").write_text("# changed by 1.1\n")

    await mgr.apply_patch_to_main(wt, commit_message="autodev: task 1.1")
    assert (repo / "README.md").read_text() == "# changed by 1.1\n"
    head_after = _head_sha(repo)
    assert head_after != head_before, "commit must advance HEAD"
    assert _head_message(repo) == "autodev: task 1.1"

    await mgr.cleanup_all()


@pytest.mark.asyncio
async def test_next_per_task_worktree_sees_prior_committed_patch(
    tmp_path: Path,
) -> None:
    """v0.25.1 Bug #2: the root motivation. After task A commits via
    ``apply_patch_to_main(commit_message=...)``, task B's per-task
    worktree (created at ``HEAD``) must contain task A's changes. This
    is the cross-task-dependency guarantee that was missing in v0.25.0
    — the unity run's Phase 2 cascade was caused by Task 2.5 starting
    from a worktree at the original HEAD where Task 2.1's prerequisites
    didn't exist."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    wt_dir = tmp_path / "execute_worktrees"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)

    # Task A — adds a new file that Task B will depend on.
    wt_a = await mgr.create_per_task("A")
    (wt_a / "shared.txt").write_text("from-A\n")
    await mgr.apply_patch_to_main(wt_a, commit_message="autodev: task A")
    await mgr.remove_per_task("A")

    # Task B — opens a fresh worktree at HEAD. The file from A must be
    # visible because A committed.
    wt_b = await mgr.create_per_task("B")
    assert (wt_b / "shared.txt").exists(), (
        "task B's worktree does not see task A's prior commit — "
        "cross-task accumulation is broken"
    )
    assert (wt_b / "shared.txt").read_text() == "from-A\n"

    await mgr.cleanup_all()


@pytest.mark.asyncio
async def test_apply_patch_to_main_commit_includes_only_diff_changes(
    tmp_path: Path,
) -> None:
    """The committed change set must reflect the worktree's diff — no
    spurious additions from main's working tree (which would mean a
    bug-prone ``git add -A`` over an unrelated dirty state)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    # Main repo working tree is clean (post _init_git_repo).

    wt_dir = tmp_path / "execute_worktrees"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)
    wt = await mgr.create_per_task("1.1")
    (wt / "added.txt").write_text("hi\n")

    await mgr.apply_patch_to_main(wt, commit_message="autodev: task 1.1")

    # Inspect the commit's changed files.
    out = subprocess.run(
        ["git", "show", "--name-only", "--pretty=", "HEAD"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )
    changed = sorted(line for line in out.stdout.splitlines() if line.strip())
    assert changed == ["added.txt"]

    await mgr.cleanup_all()


@pytest.mark.asyncio
async def test_cleanup_all_routes_per_task_via_remove_per_task(
    tmp_path: Path,
) -> None:
    """``cleanup_all`` must route per-task worktree teardown through the
    ``remove_per_task`` API (which calls ``git worktree remove`` on the
    individual subdir) rather than treating the ``tasks`` parent as a
    label and rmtreeing the whole thing in one shot."""
    import structlog

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    wt_dir = tmp_path / "tournament_dir"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)

    # Mixed layout — impl labels alongside per-task subdirs (the exact
    # layout that triggered the bug in the unity run).
    a = await mgr.create("a")
    pt_21 = await mgr.create_per_task("2.1")
    pt_23 = await mgr.create_per_task("2.3")
    assert a.exists() and pt_21.exists() and pt_23.exists()

    with structlog.testing.capture_logs() as cap:
        await mgr.cleanup_all()

    # All worktrees gone (final-state guarantee unchanged).
    assert not a.exists()
    assert not pt_21.exists()
    assert not pt_23.exists()

    per_task_events = [
        ev for ev in cap if ev.get("event") == "worktree.removed_per_task"
    ]
    removed_task_ids = {ev.get("task_id") for ev in per_task_events}
    assert removed_task_ids >= {"2.1", "2.3"}, (
        f"cleanup_all did not route per-task teardown through "
        f"remove_per_task; saw task_ids={removed_task_ids}"
    )


# ---------------------------------------------------------------------------
# v0.31.0 (Phase 2.1): _run_git CancelledError → kill child + re-raise.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_git_handles_cancellederror(tmp_path: Path) -> None:
    """When the parent task is cancelled mid-``communicate()``, ``_run_git``
    MUST kill the in-flight git child and re-raise CancelledError."""
    import asyncio
    from unittest.mock import AsyncMock, patch

    from orchestrator.worktree import _run_git

    kill_calls: list[None] = []

    async def _hang_communicate(*_a, **_kw):
        await asyncio.sleep(3600)

    proc = AsyncMock()
    proc.returncode = None
    proc.communicate = _hang_communicate
    proc.wait = AsyncMock(return_value=0)
    proc.kill = lambda: kill_calls.append(None)

    with patch(
        "orchestrator.worktree.asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        task = asyncio.create_task(_run_git(tmp_path, ["status"]))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert kill_calls, "_run_git did not kill subprocess on CancelledError"


# ---------------------------------------------------------------------------
# v0.31.0 (Phase 5.2): worktree-state manifest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_manifest_round_trip(tmp_path: Path) -> None:
    """Manifest gains an entry on create and loses it on remove.

    Round-trip:

    1. Cold start -- manifest empty.
    2. ``create_per_task`` -- one entry with the right path/task_id.
    3. ``remove_per_task`` -- entry gone.
    """
    from orchestrator import worktree_state

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    autodev_root = tmp_path / ".autodev"
    autodev_root.mkdir()
    wt_dir = autodev_root / "execute_worktrees"
    mgr = WorktreeManager(
        main_repo=repo,
        tournament_dir=wt_dir,
        autodev_root=autodev_root,
    )

    assert worktree_state.load_manifest(autodev_root) == []

    wt = await mgr.create_per_task("4.2")
    entries = worktree_state.load_manifest(autodev_root)
    assert len(entries) == 1
    e = entries[0]
    assert e.task_id == "4.2"
    assert e.label == "4.2"
    # Path should resolve to the on-disk worktree (resolve() handles symlinks).
    assert Path(e.path) == wt.resolve()
    assert e.pid_of_creator > 0
    assert e.created_at  # non-empty ISO-ish timestamp

    await mgr.remove_per_task("4.2")
    assert worktree_state.load_manifest(autodev_root) == []


def test_state_manifest_atomic_write_survives_partial_failure(
    tmp_path: Path,
) -> None:
    """Atomic write strategy: a crash during ``write_text`` leaves the
    prior manifest intact.

    We simulate this by writing a valid manifest first, then patching
    ``write_text`` on the temp file path to raise mid-write, and asserting
    the original manifest is still readable afterwards.
    """
    from unittest.mock import patch

    from orchestrator import worktree_state

    autodev_root = tmp_path / ".autodev"
    autodev_root.mkdir()
    wt_path_a = tmp_path / "execute_worktrees" / "tasks" / "1.1"
    worktree_state.record_create(
        autodev_root, path=wt_path_a, label="1.1", task_id="1.1"
    )
    before = worktree_state.load_manifest(autodev_root)
    assert len(before) == 1

    # Patch Path.write_text used by _atomic_write to fail. Because writes
    # go through the .tmp file first and only os.replace the destination
    # on success, the destination must remain unchanged.
    real_write_text = Path.write_text

    def boom(self, *args, **kwargs):
        if self.suffix == ".tmp":
            raise OSError("simulated disk full")
        return real_write_text(self, *args, **kwargs)

    with patch.object(Path, "write_text", boom):
        # Either record_create swallows the OSError silently, or it
        # raises -- either way, the manifest must not be corrupt.
        try:
            worktree_state.record_create(
                autodev_root,
                path=tmp_path / "execute_worktrees" / "tasks" / "2.2",
                label="2.2",
                task_id="2.2",
            )
        except OSError:
            pass

    after = worktree_state.load_manifest(autodev_root)
    assert after == before, "atomic write must preserve the prior manifest"
