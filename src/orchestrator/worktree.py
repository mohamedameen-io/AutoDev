"""Git worktree management for impl-tournament A/B/AB isolation.

The impl tournament needs to realize three variants (A, B, AB) as real
on-disk file states so:

1. Each variant can be independently test-engineered (fresh cwd).
2. The winning variant can be applied to the main repo via ``git apply``.
3. Losing variants leave no trace in the main repo.

Strategy: one :class:`git worktree` per variant under
``.autodev/tournaments/impl-<id>/<variant>/`` pointing at ``HEAD`` of the
main repo. The coder writes files there; ``get_diff_vs_base`` returns the
unified diff relative to ``HEAD``; ``apply_patch_to_main`` copies the
winning diff into the main worktree via ``git apply``.

All git invocations go through :func:`asyncio.create_subprocess_exec`.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from contextlib import suppress
from pathlib import Path
from typing import Iterable

from errors import AutodevError
from autologging import get_logger
from orchestrator import worktree_state


logger = get_logger(__name__)


class WorktreeError(AutodevError):
    """Any failure creating, removing, or diffing a worktree."""


class WorktreeManager:
    """Create / remove ``git worktree`` directories for one impl tournament.

    One ``WorktreeManager`` instance owns a single ``tournament_dir`` on
    disk. Worktrees are labeled (``"a"`` / ``"b"`` / ``"ab"``) and land at
    ``tournament_dir/<label>``.
    """

    def __init__(
        self,
        main_repo: Path,
        tournament_dir: Path,
        *,
        huge_mode: bool = False,
        huge_create_timeout_s: float = 600.0,
        autodev_root: Path | None = None,
    ) -> None:
        """Initialize a worktree manager.

        v0.22.1 A3: ``huge_mode`` flag from ``runtime.repo_probe.is_huge``
        extends the ``git worktree add`` timeout from 60 s to
        ``huge_create_timeout_s`` (default 600 s). On Unity-scale repos
        (358K files, 3 GB) full-checkout worktree creation can take
        80-180 s; the legacy 60 s ceiling killed it. Full sparse-by-default
        lands in v0.23.0 C1.
        """
        self._main = Path(main_repo)
        self._dir = Path(tournament_dir)
        self._huge_mode = bool(huge_mode)
        self._huge_create_timeout_s = float(huge_create_timeout_s)
        # v0.31.0 (Phase 5.2): root for the worktree-state manifest.
        # When None we infer ``<main_repo>/.autodev/`` so legacy callers
        # continue recording state without an explicit override. Tests
        # (and out-of-tree callers) can pass an explicit path.
        if autodev_root is None:
            self._autodev_root = self._main / ".autodev"
        else:
            self._autodev_root = Path(autodev_root)
        self._log = get_logger(
            component="worktree",
            main_repo=str(self._main),
            tournament_dir=str(self._dir),
        )

    def _create_timeout_s(self) -> float:
        """Per-call timeout for slow ``git worktree add`` operations.

        Returns ``huge_create_timeout_s`` when ``huge_mode`` is on,
        otherwise the historical 60 s default.
        """
        return self._huge_create_timeout_s if self._huge_mode else 60.0

    @property
    def main_repo(self) -> Path:
        return self._main

    @property
    def tournament_dir(self) -> Path:
        return self._dir

    def worktree_path(self, label: str) -> Path:
        """Return the on-disk path for a worktree with ``label``.

        The label is used verbatim (callers pass ``"a"`` / ``"b"`` / ``"ab"``).
        """
        return self._dir / label

    # ── Creation / removal ─────────────────────────────────────────────────

    async def create(
        self,
        label: str,
        base_ref: str = "HEAD",
        sparse_paths: list[str] | None = None,
    ) -> Path:
        """Create a new git worktree at ``tournament_dir/<label>``.

        Uses ``git worktree add --detach <path> <base_ref>`` so the worktree
        is not associated with any branch (matches short-lived use — nothing
        to conflict on branch names across parallel tournaments).

        v0.17.0 S6: ``sparse_paths`` is an optional list of repo-relative
        path prefixes. When non-empty AND git ≥2.25 is available:

        1. ``git worktree add --no-checkout`` skips the initial materialization.
        2. ``git sparse-checkout init --cone`` enables cone-mode (faster +
           well-tested vs. legacy non-cone mode).
        3. ``git sparse-checkout set <prefixes>`` narrows the working set.
        4. ``git checkout`` materializes the narrowed files on disk.

        Falls back to a full checkout (with a warning) when:

        * ``sparse_paths`` is None or empty (caller opted out).
        * git is older than 2.25 (cone-mode unavailable).

        Returns the worktree path. Raises :class:`WorktreeError` on failure.
        """
        wt = self.worktree_path(label)
        self._dir.mkdir(parents=True, exist_ok=True)
        if wt.exists():
            raise WorktreeError(
                f"worktree path {wt} already exists; call remove() first"
            )

        # v0.17.0 S6: sparse-checkout pre-flight.
        # Empty list is treated as None (defensive — callers may forward
        # phase.edit_scope which is sometimes legitimately empty).
        use_sparse = bool(sparse_paths)
        if use_sparse:
            ver = await _get_git_version(self._main)
            if ver < (2, 25, 0):
                self._log.warning(
                    "worktree.sparse_checkout.git_too_old",
                    version=".".join(str(p) for p in ver),
                    fallback="full checkout",
                )
                use_sparse = False

        if use_sparse:
            # Step 1: create worktree without materializing files.
            rc, out, err = await _run_git(
                self._main,
                ["worktree", "add", "--no-checkout", "--detach", str(wt), base_ref],
                timeout_s=self._create_timeout_s(),
            )
            if rc != 0:
                raise WorktreeError(
                    f"git worktree add --no-checkout failed (rc={rc}): "
                    f"{err.strip() or out.strip()}"
                )
            # Step 2: enable cone-mode sparse-checkout in the new worktree.
            rc, out, err = await _run_git(
                wt, ["sparse-checkout", "init", "--cone"]
            )
            if rc != 0:
                raise WorktreeError(
                    f"git sparse-checkout init --cone failed (rc={rc}): "
                    f"{err.strip() or out.strip()}"
                )
            # Step 3: narrow to the requested prefixes.
            assert sparse_paths is not None  # narrowed by ``use_sparse`` above
            rc, out, err = await _run_git(
                wt, ["sparse-checkout", "set", *sparse_paths]
            )
            if rc != 0:
                raise WorktreeError(
                    f"git sparse-checkout set failed (rc={rc}): "
                    f"{err.strip() or out.strip()}"
                )
            # Step 4: materialize the narrowed working set.
            rc, out, err = await _run_git(wt, ["checkout"])
            if rc != 0:
                raise WorktreeError(
                    f"git checkout (sparse) failed (rc={rc}): "
                    f"{err.strip() or out.strip()}"
                )
            self._log.info(
                "worktree.created_sparse",
                label=label,
                path=str(wt),
                paths=sparse_paths,
            )
            worktree_state.record_create(
                self._autodev_root, path=wt, label=label, task_id=None
            )
            return wt

        rc, out, err = await _run_git(
            self._main,
            ["worktree", "add", "--detach", str(wt), base_ref],
            timeout_s=self._create_timeout_s(),
        )
        if rc != 0:
            raise WorktreeError(
                f"git worktree add failed (rc={rc}): {err.strip() or out.strip()}"
            )
        self._log.info("worktree.created", label=label, path=str(wt))
        worktree_state.record_create(
            self._autodev_root, path=wt, label=label, task_id=None
        )
        return wt

    async def create_per_task(
        self,
        task_id: str,
        base_ref: str = "HEAD",
        sparse_paths: list[str] | None = None,
    ) -> Path:
        """Create a per-task worktree at ``tournament_dir/tasks/<task_id>``.

        v0.11.0 convenience for the parallel execute_phase dispatcher.
        Routes the worktree under a ``tasks/`` subdirectory so the
        layout is unambiguous when both impl-tournament variant
        worktrees AND per-task isolation worktrees coexist on disk:

        * impl tournament: ``tournament_dir/{a,b,ab}``
        * per-task isolation: ``tournament_dir/tasks/{task_id}``

        v0.17.0 S6: ``sparse_paths`` is forwarded into the same
        sparse-checkout machinery used by :meth:`create`. ``None`` (or
        an empty list) preserves legacy full-checkout behavior.
        """
        wt = self._dir / "tasks" / task_id
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / "tasks").mkdir(parents=True, exist_ok=True)
        if wt.exists():
            raise WorktreeError(
                f"per-task worktree path {wt} already exists; "
                "call remove_per_task() first"
            )

        use_sparse = bool(sparse_paths)
        if use_sparse:
            ver = await _get_git_version(self._main)
            if ver < (2, 25, 0):
                self._log.warning(
                    "worktree.sparse_checkout.git_too_old",
                    task_id=task_id,
                    version=".".join(str(p) for p in ver),
                    fallback="full checkout",
                )
                use_sparse = False

        if use_sparse:
            rc, out, err = await _run_git(
                self._main,
                ["worktree", "add", "--no-checkout", "--detach", str(wt), base_ref],
            )
            if rc != 0:
                raise WorktreeError(
                    f"git worktree add --no-checkout failed (rc={rc}): "
                    f"{err.strip() or out.strip()}"
                )
            for cmd in (
                ["sparse-checkout", "init", "--cone"],
                ["sparse-checkout", "set", *(sparse_paths or [])],
                ["checkout"],
            ):
                rc, out, err = await _run_git(wt, cmd)
                if rc != 0:
                    raise WorktreeError(
                        f"git {' '.join(cmd)} failed (rc={rc}): "
                        f"{err.strip() or out.strip()}"
                    )
            self._log.info(
                "worktree.created_per_task_sparse",
                task_id=task_id,
                path=str(wt),
                paths=sparse_paths,
            )
            worktree_state.record_create(
                self._autodev_root,
                path=wt,
                label=task_id,
                task_id=task_id,
            )
            return wt

        rc, out, err = await _run_git(
            self._main,
            ["worktree", "add", "--detach", str(wt), base_ref],
        )
        if rc != 0:
            raise WorktreeError(
                f"git worktree add failed (rc={rc}): {err.strip() or out.strip()}"
            )
        self._log.info("worktree.created_per_task", task_id=task_id, path=str(wt))
        worktree_state.record_create(
            self._autodev_root,
            path=wt,
            label=task_id,
            task_id=task_id,
        )
        return wt

    async def remove_per_task(self, task_id: str, force: bool = True) -> None:
        """Remove a per-task worktree (created by :meth:`create_per_task`).

        Defaults to ``force=True`` — workers commonly leave dirty
        worktrees (uncommitted helpful files, .pytest_cache/, etc.) and
        the per-task isolation contract is "delete unconditionally on
        worker exit".
        """
        wt = self._dir / "tasks" / task_id
        if not wt.exists():
            await _run_git(self._main, ["worktree", "prune"])
            worktree_state.record_cleanup(self._autodev_root, path=wt)
            return

        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(wt))
        rc, _, _ = await _run_git(self._main, args)
        if rc != 0:
            await self._force_remove(wt)
            worktree_state.record_cleanup(self._autodev_root, path=wt)
            return
        self._log.info("worktree.removed_per_task", task_id=task_id)
        worktree_state.record_cleanup(self._autodev_root, path=wt)

    async def remove(self, label: str, force: bool = False) -> None:
        """Remove a worktree and its on-disk directory.

        First attempts the clean ``git worktree remove`` path. If that fails
        (uncommitted edits, corruption) and ``force=True``, falls back to
        ``git worktree remove --force`` + filesystem ``shutil.rmtree`` and
        a final ``git worktree prune`` to clean stale metadata.

        The literal label ``"tasks"`` is reserved: it names the parent
        container for per-task worktrees created via :meth:`create_per_task`.
        Routing it through ``remove`` would fall through to
        ``shutil.rmtree(<dir>/tasks)`` and destroy every sibling per-task
        worktree (regression fixed in v0.25.1). Use :meth:`remove_per_task`
        for per-task teardown instead.
        """
        if label == "tasks":
            raise WorktreeError(
                "remove() refused reserved label 'tasks' — this is the "
                "parent container for per-task worktrees, not a worktree "
                "itself. Use remove_per_task(task_id) instead."
            )
        wt = self.worktree_path(label)
        if not wt.exists():
            # Best-effort prune so the admin DB is consistent if a previous
            # remove failed mid-way.
            await _run_git(self._main, ["worktree", "prune"])
            worktree_state.record_cleanup(self._autodev_root, path=wt)
            return

        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(wt))
        rc, out, err = await _run_git(self._main, args)
        if rc != 0:
            if not force:
                # Retry forcefully — this is the common happy path for our
                # tournament where worktrees may have uncommitted changes.
                await self._force_remove(wt)
                worktree_state.record_cleanup(self._autodev_root, path=wt)
                return
            # Force flag was already set — escalate to rmtree + prune.
            await self._force_remove(wt)
            worktree_state.record_cleanup(self._autodev_root, path=wt)
            return
        self._log.info("worktree.removed", label=label, path=str(wt))
        worktree_state.record_cleanup(self._autodev_root, path=wt)

    async def _force_remove(self, wt: Path) -> None:
        """Fallback cleanup when ``git worktree remove`` can't finish.

        Defensive guard (v0.25.1): refuse to rmtree the reserved
        ``<tournament_dir>/tasks`` parent directory. Doing so would
        destroy every in-flight per-task worktree at once. Callers must
        hand individual worktree paths only; per-task removal goes
        through :meth:`remove_per_task` which targets
        ``<tournament_dir>/tasks/<task_id>``.
        """
        if wt.name == "tasks" and wt.parent == self._dir:
            raise WorktreeError(
                f"_force_remove() refused to rmtree per-task parent "
                f"directory {wt!r}; doing so would destroy every "
                f"in-flight per-task worktree. Use remove_per_task() "
                f"for per-task teardown."
            )
        # First try git's own force path (handles admin DB).
        await _run_git(
            self._main,
            ["worktree", "remove", "--force", str(wt)],
        )
        if wt.exists():
            # Filesystem fallback.
            try:
                shutil.rmtree(wt, ignore_errors=True)
            except OSError:
                pass
        # Always prune admin state afterwards.
        await _run_git(self._main, ["worktree", "prune"])
        self._log.warning("worktree.force_removed", path=str(wt))

    async def cleanup_all(self) -> None:
        """Remove every worktree under ``tournament_dir`` and the dir itself.

        Two on-disk layers are swept:

        * Top-level impl-tournament label worktrees (``a`` / ``b`` /
          ``ab``) — removed via :meth:`remove`.
        * Per-task worktrees under ``tasks/<task_id>`` (created by
          :meth:`create_per_task`) — removed via :meth:`remove_per_task`.

        The ``tasks`` parent directory is **never** passed to
        :meth:`remove` (regression fixed in v0.25.1). Treating it as a
        label would fall through to ``shutil.rmtree(<dir>/tasks)`` and
        destroy every sibling per-task worktree in one call — including
        any with un-applied patches.

        Safe to call twice — subsequent calls are no-ops.
        """
        if not self._dir.exists():
            return

        # Layer 1: top-level impl-tournament label worktrees. The
        # reserved ``tasks`` subdirectory is a container for per-task
        # worktrees, not a worktree itself — handled in layer 2.
        labels = [
            p.name
            for p in self._dir.iterdir()
            if p.is_dir() and p.name != "tasks"
        ]
        for lbl in labels:
            try:
                await self.remove(lbl, force=True)
            except WorktreeError as exc:
                # Swallow — we're in cleanup, best effort only.
                self._log.warning(
                    "worktree.cleanup_remove_failed", label=lbl, err=str(exc)
                )

        # Layer 2: per-task worktrees under tasks/<task_id>.
        tasks_dir = self._dir / "tasks"
        if tasks_dir.exists():
            task_ids = [p.name for p in tasks_dir.iterdir() if p.is_dir()]
            for tid in task_ids:
                try:
                    await self.remove_per_task(tid, force=True)
                except WorktreeError as exc:
                    self._log.warning(
                        "worktree.cleanup_remove_per_task_failed",
                        task_id=tid,
                        err=str(exc),
                    )

        # Remove whatever is left on disk (history.json, pass_NN/, empty
        # ``tasks/`` shell, etc).
        try:
            shutil.rmtree(self._dir, ignore_errors=True)
        except OSError:
            pass
        # Final prune.
        await _run_git(self._main, ["worktree", "prune"])
        self._log.info("worktree.cleanup_complete")

    async def expand_sparse_paths(
        self, worktree: Path, additional_paths: list[str]
    ) -> None:
        """v0.20.0 C3: dynamically widen a sparse-checkout worktree.

        Invokes ``git -C <worktree> sparse-checkout add <prefixes>`` then
        ``git -C <worktree> checkout`` to materialize the newly admitted
        paths. Idempotent — paths already in the sparse set are no-ops
        for git's add subcommand.

        Used by :func:`orchestrator.execute_phase` when a developer
        reports a missing-file error: if the missing path is covered by
        ``task.extended_scope`` / ``phase.edit_scope`` / ``plan.edit_scope``
        but the sparse worktree never materialized it, expand and retry
        once.

        Silently no-ops when:

        * ``additional_paths`` is empty / falsy.
        * the worktree is not a sparse-checkout (``sparse-checkout add``
          will fail with a benign warning).
        * git is older than 2.25 (cone-mode unsupported).
        """
        if not additional_paths:
            return
        if not worktree.exists():
            raise WorktreeError(
                f"cannot expand sparse paths in missing worktree {worktree}"
            )
        # Pre-flight: only run on git >=2.25 (cone-mode requirement).
        ver = await _get_git_version(self._main)
        if ver < (2, 25, 0):
            self._log.warning(
                "worktree.sparse_expand.git_too_old",
                version=".".join(str(p) for p in ver),
            )
            return
        rc, out, err = await _run_git(
            worktree, ["sparse-checkout", "add", *additional_paths]
        )
        if rc != 0:
            # Non-fatal: log and bail (best-effort widen).
            self._log.warning(
                "worktree.sparse_expand.add_failed",
                paths=additional_paths,
                rc=rc,
                err=err.strip() or out.strip(),
            )
            return
        rc2, out2, err2 = await _run_git(worktree, ["checkout"])
        if rc2 != 0:
            self._log.warning(
                "worktree.sparse_expand.checkout_failed",
                rc=rc2,
                err=err2.strip() or out2.strip(),
            )
            return
        self._log.info(
            "worktree.sparse_expanded", paths=additional_paths, worktree=str(worktree)
        )

    # ── Diffing / patching ─────────────────────────────────────────────────

    async def get_diff_vs_base(
        self, worktree: Path, base_ref: str = "HEAD"
    ) -> str:
        """Return unified diff from ``base_ref`` to the worktree's content.

        Uses ``git diff --no-color <base_ref>`` run with ``cwd=worktree`` so
        both tracked-modified AND untracked new files are represented. Any
        untracked files are intentionally included via a second ``git diff
        --no-index`` pass for each.
        """
        if not worktree.exists():
            raise WorktreeError(f"worktree {worktree} does not exist")

        # 1. Diff for tracked changes (including staged) against base_ref.
        rc, out, err = await _run_git(
            worktree,
            ["diff", "--no-color", base_ref],
        )
        if rc != 0:
            raise WorktreeError(
                f"git diff failed (rc={rc}): {err.strip() or out.strip()}"
            )
        diff_text = out

        # 2. Add untracked files — git diff ignores them by default.
        untracked = await self._list_untracked(worktree)
        for rel in untracked:
            rc2, out2, _ = await _run_git(
                worktree,
                [
                    "diff",
                    "--no-color",
                    "--no-index",
                    "/dev/null",
                    rel,
                ],
            )
            # git diff --no-index returns 1 for "files differ" (success).
            if rc2 in (0, 1):
                diff_text += out2

        return diff_text

    async def _list_untracked(self, worktree: Path) -> list[str]:
        """Return paths of untracked files (excluding gitignored)."""
        rc, out, _ = await _run_git(
            worktree,
            ["ls-files", "--others", "--exclude-standard"],
        )
        if rc != 0:
            return []
        return [line for line in out.splitlines() if line.strip()]

    async def apply_patch_to_main(
        self,
        worktree: Path,
        base_ref: str = "HEAD",
        three_way: bool = False,
        edit_scope: list[str] | None = None,
        commit_message: str | None = None,
    ) -> None:
        """Apply the worktree's diff to the main repo's working tree.

        Strategy: compute ``get_diff_vs_base(worktree)`` then pipe to
        ``git apply`` from the main repo. Raises :class:`WorktreeError` on
        any apply conflict so the caller can surface a helpful error rather
        than leave the main repo half-patched.

        v0.11.0: ``three_way=True`` adds ``--3way`` to ``git apply`` so
        conflicts fall back to git's merge machinery instead of patch.
        Used by the conflict-escalation path after ``critic_sounding_board``
        returns ``RESOLUTION: rebase-and-retry``.

        v0.14.0: ``edit_scope`` is an optional list of repo-relative path
        prefixes. When non-empty, every diff hunk's target path MUST lie
        under one of the prefixes; if any path is out-of-scope, the apply
        is aborted with :class:`orchestrator.dag.EditScopeViolation`
        BEFORE any ``git apply`` runs (so main is never half-patched).
        ``None`` / empty list preserves legacy whole-repo behavior.

        v0.25.1 Bug #2: ``commit_message`` enables persistent integration.
        When supplied, the apply uses ``git apply --index`` so hunks are
        staged as they land, and a follow-on ``git commit`` records the
        change set on the main branch. Subsequent ``create_per_task``
        calls (which default ``base_ref="HEAD"``) see the new commit,
        unlocking cross-task dependencies. ``None`` (default) preserves
        the v0.25.0 working-tree-only behavior used by impl tournaments.
        """
        diff_text = await self.get_diff_vs_base(worktree, base_ref=base_ref)
        if not diff_text.strip():
            self._log.info("worktree.apply_patch.empty_diff")
            return

        # v0.14.0: pre-flight scope check before any git apply runs.
        if edit_scope:
            from adapters.git_utils import extract_files_from_diff
            from orchestrator.dag import EditScopeViolation, is_in_scope

            files_in_diff = extract_files_from_diff(diff_text)
            for fp in files_in_diff:
                if not is_in_scope(fp, edit_scope):
                    raise EditScopeViolation(
                        f"diff hunk targets out-of-scope file {fp!r}; "
                        f"resolved edit_scope = {edit_scope!r}"
                    )

        check_args = ["apply", "--check"]
        apply_args = ["apply"]
        if three_way:
            check_args.append("--3way")
            apply_args.append("--3way")
        if commit_message is not None:
            # v0.25.1 Bug #2: stage as we apply so the follow-on commit
            # captures exactly the diff's hunks (no risk of sweeping
            # unrelated dirty state via a later ``git add -A``).
            apply_args.append("--index")

        # Pre-flight: ``git apply --check`` so we fail fast on conflicts.
        check_rc, _, check_err = await _run_git(
            self._main,
            check_args,
            stdin=diff_text,
        )
        if check_rc != 0:
            raise WorktreeError(
                "cannot apply tournament winner to main repo "
                f"(conflict in working tree?): {check_err.strip()}"
            )
        apply_rc, _, apply_err = await _run_git(
            self._main,
            apply_args,
            stdin=diff_text,
        )
        if apply_rc != 0:
            raise WorktreeError(
                f"git apply failed (rc={apply_rc}): {apply_err.strip()}"
            )
        self._log.info(
            "worktree.apply_patch.success",
            diff_bytes=len(diff_text),
            three_way=three_way,
            committed=commit_message is not None,
        )

        # v0.25.1 Bug #2: persistent integration via commit-per-task.
        if commit_message is None:
            return
        # Sanity: ``--index`` should have staged hunks. If the staged
        # diff is empty (apply was a content-identical no-op), skip the
        # commit rather than producing an empty change set.
        staged_rc, _, _ = await _run_git(
            self._main, ["diff", "--cached", "--quiet"]
        )
        if staged_rc == 0:
            self._log.info(
                "worktree.apply_patch.no_staged_changes",
                commit_message=commit_message,
            )
            return
        commit_rc, _, commit_err = await _run_git(
            self._main,
            ["commit", "-m", commit_message, "--no-verify"],
        )
        if commit_rc != 0:
            raise WorktreeError(
                f"git commit after apply failed (rc={commit_rc}): "
                f"{commit_err.strip()}"
            )
        self._log.info(
            "worktree.apply_patch.committed",
            commit_message=commit_message,
        )


# ── Helpers ─────────────────────────────────────────────────────────────


_MISSING_FILE_PATTERNS = (
    # python: "FileNotFoundError: [Errno 2] No such file or directory: '<path>'"
    # Quoted form must come BEFORE the bare-path pattern below — otherwise
    # the bare pattern eats the closing quote as part of the path.
    re.compile(
        r"No such file or directory:\s*['\"]([^'\"\n]+)['\"]",
    ),
    # generic: "cannot open '<path>'"
    re.compile(r"cannot open\s+['\"]([^'\"\n]+)['\"]", re.IGNORECASE),
    # cat / head / bash style: "<context: >?<path>: No such file or directory"
    # ``bash: src/foo/bar.py: No such file or directory`` — match the
    # token immediately preceding the colon-No-such-file substring.
    re.compile(
        r"(?:^|\s)([^\s:'\"]+):\s*No such file or directory",
        re.IGNORECASE,
    ),
)


def detect_missing_paths(text: str) -> list[str]:
    """v0.20.0 C3: extract repo-relative-looking paths from adapter output.

    Pattern-matches several common shapes for "file not found" errors
    that bubble up through CLI tooling. Returns a deduplicated list
    preserving first-seen order. Absolute paths and ``.``/``..``
    segments are filtered out — only repo-relative-looking paths flow
    through (they're the only ones the sparse-checkout add can act on).
    """
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for pattern in _MISSING_FILE_PATTERNS:
        for match in pattern.finditer(text):
            path = match.group(1).strip()
            if not path or path in seen:
                continue
            if path.startswith("/"):
                continue
            parts = path.split("/")
            if any(p == ".." for p in parts):
                continue
            seen.add(path)
            found.append(path)
    return found


async def _get_git_version(cwd: Path) -> tuple[int, int, int]:
    """Return the local ``git`` binary's version as ``(major, minor, patch)``.

    Used by :meth:`WorktreeManager.create` to gate the sparse-checkout
    cone-mode path: the ``--cone`` flag only landed in git 2.25.

    Returns ``(0, 0, 0)`` on any parse / launch failure — callers
    should treat that as "older than the threshold" and fall back to
    the full-checkout path.
    """
    try:
        rc, out, _ = await _run_git(cwd, ["--version"])
    except WorktreeError:
        return (0, 0, 0)
    if rc != 0:
        return (0, 0, 0)
    # Output looks like ``git version 2.40.1`` (or ``git version 2.40.1.windows.1``).
    text = out.strip()
    parts = text.split()
    if len(parts) < 3:
        return (0, 0, 0)
    nums = parts[2].split(".")
    try:
        major = int(nums[0])
        minor = int(nums[1]) if len(nums) > 1 else 0
        patch = int(nums[2]) if len(nums) > 2 else 0
        return (major, minor, patch)
    except (ValueError, IndexError):
        return (0, 0, 0)


async def _run_git(
    cwd: Path,
    args: Iterable[str],
    stdin: str | None = None,
    timeout_s: float = 60.0,
) -> tuple[int, str, str]:
    """Run ``git <args>`` in ``cwd``. Returns (rc, stdout, stderr).

    Timeout defaults to 60 s — suitable for local worktree ops. Raises
    :class:`WorktreeError` on timeout / subprocess launch failure.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd),
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as exc:
        raise WorktreeError(f"failed to launch git: {exc}") from exc

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(stdin.encode("utf-8") if stdin is not None else None),
            timeout=timeout_s,
        )
    except asyncio.CancelledError:
        # v0.31.0 (Phase 2.1): parent task cancelled (SIGTERM,
        # KeyboardInterrupt propagation, or asyncio.gather cancel).
        # Kill the in-flight git child so we don't leak processes after
        # the orchestrator exits — mirrors claude_code.py:171-180.
        with suppress(ProcessLookupError):
            proc.kill()
        with suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=5)
        raise
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise WorktreeError(
            f"git {' '.join(args)} timed out after {timeout_s}s"
        ) from exc

    rc = proc.returncode if proc.returncode is not None else -1
    return (
        rc,
        stdout_b.decode("utf-8", errors="replace"),
        stderr_b.decode("utf-8", errors="replace"),
    )


__all__ = ["WorktreeError", "WorktreeManager", "detect_missing_paths"]
