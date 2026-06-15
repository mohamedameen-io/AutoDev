"""v0.21.0 A1: warm-start pool wrapping :class:`WorktreeManager`.

Cold-starts ``N`` worktrees concurrently at orchestrator init then recycles
them via ``git reset --hard <baseline> && git clean -fdx`` instead of
paying ``git worktree add`` cost on every task dispatch. The pool reuses
all of :class:`WorktreeManager`'s plumbing — including sparse-checkout
support — but adds:

* a baseline commit captured per claim so reset is deterministic,
* a queue of available worktrees,
* a fallback to lazy ``WorktreeManager.create_per_task`` when the pool
  is exhausted,
* a ``cleanup_all`` that removes every pooled worktree AND the persistent
  pool dir.

Persistent dir: ``<autodev_root>/execute_worktrees_pool/`` (gitignored
under ``.autodev/`` per the project's ``.gitignore``).

Opt-in via ``cfg.worktree_pool_enabled`` and integration into
:func:`orchestrator.execute_phase.run_execute_phase`. When the flag is
False (default), the existing :class:`WorktreeManager` lazy-create path
is used unchanged.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from autologging import get_logger
from orchestrator.worktree import (
    WorktreeError,
    WorktreeManager,
    _run_git,
)


logger = get_logger(__name__)


class WorktreePool:
    """Recyclable pool of git worktrees for cross-task isolation.

    Wraps a :class:`WorktreeManager` rooted at
    ``<autodev_root>/execute_worktrees_pool/``. The pool's lifecycle:

    1. :meth:`cold_start(n)` — concurrently pre-create ``n`` worktrees
       under labels ``pool-0`` … ``pool-(n-1)`` and push their paths
       into an internal ``asyncio.Queue``.
    2. :meth:`claim()` — pop one path from the queue. If the queue is
       empty, fall back to a lazy ``WorktreeManager.create_per_task``
       under ``pool-overflow-<task_id>`` so the dispatcher never blocks.
    3. :meth:`release(task_id)` — reset the worktree (``git reset --hard
       <baseline>`` + ``git clean -fdx``) and push it back to the queue.
       Idempotent if already at baseline.
    4. :meth:`cleanup_all()` — remove every worktree (pooled + overflow)
       and the persistent pool dir. Safe to call twice.

    Baseline commit is the SHA captured on the orchestrator's main repo
    at the time of cold-start. Re-using ``git reset --hard <baseline>``
    on every release ensures every claim sees a clean working tree even
    if a previous claim crashed mid-edit.
    """

    def __init__(
        self,
        main_repo: Path,
        pool_dir: Path,
        size: int,
    ) -> None:
        self._main = Path(main_repo)
        self._pool_dir = Path(pool_dir)
        self._size = max(0, int(size))
        # Re-use WorktreeManager's create / remove / sparse plumbing.
        self._mgr = WorktreeManager(
            main_repo=self._main, tournament_dir=self._pool_dir
        )
        # asyncio.Queue is created lazily in cold_start; it must be bound
        # to a running loop. ``None`` means "not yet cold-started".
        self._available: asyncio.Queue[Path] | None = None
        # Map from worktree path → label, used by release() to reset
        # by path while remove() takes a label.
        self._claimed: dict[str, str] = {}
        # Map from claimed path → owning task_id for forensics.
        self._claim_task: dict[str, str] = {}
        # C4: explicit reverse index task_id → claimed path. Maintained in
        # lockstep with ``_claim_task`` so ``remove_per_task`` does an O(1)
        # direct lookup instead of an O(n) racy reverse scan of ``_claim_task``
        # (which mis-assigned worktrees when two corrective tasks raced).
        self._task_to_path: dict[str, str] = {}
        # C4: serialize the read-modify-write across the queue + both claim
        # maps so concurrent corrective tasks (ids like '1.c4'/'1.c5') can
        # never double-assign the same pooled path or leave ``_claimed`` and
        # ``_claim_task`` diverged. asyncio.Lock binds to the running loop on
        # first ``async with``; safe to construct here (no loop required).
        self._claim_lock = asyncio.Lock()
        # Baseline commit — captured at cold_start. Empty until then.
        self._baseline: str = ""
        # Tracks every label this pool created so cleanup_all can remove
        # them even if the queue was drained externally.
        self._all_labels: set[str] = set()
        self._log = get_logger(
            component="worktree_pool",
            main_repo=str(self._main),
            pool_dir=str(self._pool_dir),
            size=self._size,
        )

    @property
    def size(self) -> int:
        return self._size

    @property
    def baseline_commit(self) -> str:
        """Return the baseline SHA captured at cold-start (empty before)."""
        return self._baseline

    async def cold_start(self, n: int | None = None) -> None:
        """Pre-create ``n`` worktrees concurrently.

        ``n=None`` defaults to the pool's configured size. Idempotent: a
        second call after a successful cold-start is a no-op (the queue
        is already populated).

        Captures ``self._baseline`` as the main repo's HEAD SHA before
        any worktree is created. This is the reset target on every
        :meth:`release`, so even if HEAD advances during execution
        (e.g. an in-flight task commits), the pool keeps recycling
        against the original baseline — predictable, side-effect-free
        diff generation.
        """
        if self._available is None:
            self._available = asyncio.Queue()
        if self._available.qsize() > 0 or self._claimed:
            # Already cold-started.
            return

        # Capture baseline once — every reset uses this SHA.
        self._baseline = await self._capture_baseline()

        target = self._size if n is None else max(0, int(n))
        if target == 0:
            self._log.info("worktree_pool.cold_start.skipped_zero")
            return

        async def create_one(idx: int) -> Path | None:
            label = f"pool-{idx}"
            try:
                p = await self._mgr.create(label, base_ref=self._baseline or "HEAD")
            except WorktreeError as exc:
                self._log.warning(
                    "worktree_pool.cold_start.create_failed",
                    idx=idx,
                    err=str(exc),
                )
                return None
            self._all_labels.add(label)
            return p

        results = await asyncio.gather(
            *(create_one(i) for i in range(target))
        )
        created = [p for p in results if p is not None]
        for p in created:
            await self._available.put(p)
        self._log.info(
            "worktree_pool.cold_start.complete",
            requested=target,
            created=len(created),
            baseline=self._baseline[:12] if self._baseline else "<none>",
        )

    async def claim(self, task_id: str | None = None) -> Path:
        """Pop one worktree path from the queue, or lazily create overflow.

        Returns a worktree path on disk. The path's working tree may have
        residue from a previous claim if :meth:`release` was not called
        — but pool releases reset to baseline, so the only residue path
        is "pool was new, never released" (which is also baseline) or
        "previous claim crashed" (handled by the caller's per-task
        cleanup logic).

        ``task_id`` is recorded for forensics + overflow naming.
        """
        if self._available is None:
            # Caller didn't cold-start — degrade to overflow path so
            # the orchestrator never blocks if cold-start raced with
            # dispatch.
            self._available = asyncio.Queue()

        # C4: the queue pop AND both map writes happen under one lock so a
        # second concurrent claim can never observe a half-updated state and
        # hand out a path that's already been (or is being) assigned. Pinned
        # to the FULL task_id per [[explicit-identity]].
        async with self._claim_lock:
            if not self._available.empty():
                path = self._available.get_nowait()
                label = self._label_for(path)
                self._record_claim(path, label, task_id)
                self._log.info(
                    "worktree_pool.claim.from_queue",
                    label=label,
                    path=str(path),
                    task_id=task_id,
                )
                return path

        # Pool exhausted — create an overflow worktree lazily. Uses the
        # per-task subdir under tournament_dir/tasks/<id>. The create itself
        # is outside the lock (it's a slow git+disk op and creates a fresh,
        # task-scoped path that no other claim can collide on); only the
        # bookkeeping write below re-enters the lock.
        if task_id is None:
            async with self._claim_lock:
                task_id = f"overflow-{len(self._claimed)}"
        path = await self._mgr.create_per_task(
            task_id, base_ref=self._baseline or "HEAD"
        )
        # Per-task worktrees live under pool_dir/tasks/<task_id>; their
        # "label" for our purposes is the task_id. We track them in
        # _claimed so release() / cleanup_all can find them.
        async with self._claim_lock:
            self._record_claim(path, f"tasks/{task_id}", task_id)
        self._log.info(
            "worktree_pool.claim.overflow",
            task_id=task_id,
            path=str(path),
        )
        return path

    async def release(self, worktree: Path, task_id: str | None = None) -> None:
        """Reset a worktree to baseline and push back to the queue.

        Reset semantics:
        * ``git reset --hard <baseline>`` — discard all tracked changes.
        * ``git clean -fdx`` — discard all untracked + ignored files
          (impl tournaments commonly leave .pytest_cache/, build
          artifacts, etc.).

        Idempotent: a worktree already at baseline emits a no-op reset
        with the same SHA.

        Overflow worktrees (created lazily under tasks/<task_id>) are
        REMOVED rather than queued — they're outside the cold-start
        budget and re-using them would inflate the queue beyond
        ``size``.
        """
        if self._available is None:
            self._available = asyncio.Queue()
        path_str = str(worktree)

        # C4: pop both maps atomically and verify the path was claimed by the
        # task_id we're releasing for (explicit-identity guard). A mismatch
        # means a concurrent claim overwrote this path's owner — warn loudly
        # but still proceed with teardown so we never leak the worktree.
        async with self._claim_lock:
            label = self._claimed.pop(path_str, None)
            owner = self._claim_task.pop(path_str, None)
            # Keep the reverse index consistent: drop whatever task_id mapped
            # to this path (the recorded owner, not necessarily ``task_id``).
            if owner is not None:
                self._task_to_path.pop(owner, None)
            if task_id is not None and owner is not None and owner != task_id:
                self._task_to_path.pop(task_id, None)
        if (
            task_id is not None
            and owner is not None
            and owner != task_id
        ):
            self._log.warning(
                "worktree_pool.release.task_identity_mismatch",
                path=path_str,
                claimed_by=owner,
                releasing_as=task_id,
            )

        if not worktree.exists():
            self._log.warning(
                "worktree_pool.release.path_missing", path=path_str
            )
            return

        # Reset: hard-reset to baseline, then clean.
        if self._baseline:
            rc, out, err = await _run_git(
                worktree, ["reset", "--hard", self._baseline]
            )
            if rc != 0:
                self._log.warning(
                    "worktree_pool.release.reset_failed",
                    rc=rc,
                    err=(err or out).strip(),
                    path=path_str,
                )
                # Best-effort: if reset failed, drop this worktree
                # entirely rather than re-queue corrupt state.
                if label is not None:
                    try:
                        if label.startswith("tasks/"):
                            tid = label.split("/", 1)[1]
                            await self._mgr.remove_per_task(tid)
                        else:
                            await self._mgr.remove(label, force=True)
                    except WorktreeError:
                        pass
                return
        rc2, out2, err2 = await _run_git(worktree, ["clean", "-fdx"])
        if rc2 != 0:
            self._log.warning(
                "worktree_pool.release.clean_failed",
                rc=rc2,
                err=(err2 or out2).strip(),
                path=path_str,
            )
            # Continue — clean failure is recoverable; future claims will
            # see residue but the reset already restored tracked state.

        # Overflow worktrees (tasks/<id>) are not queued — they're one-off.
        if label is not None and label.startswith("tasks/"):
            try:
                tid = label.split("/", 1)[1]
                await self._mgr.remove_per_task(tid)
            except WorktreeError as exc:
                self._log.warning(
                    "worktree_pool.release.overflow_remove_failed",
                    label=label,
                    err=str(exc),
                )
            return

        async with self._claim_lock:
            await self._available.put(worktree)
        self._log.info(
            "worktree_pool.release.queued",
            label=label or self._label_for(worktree),
            path=path_str,
            task_id=task_id,
        )

    # ── WorktreeManager-compatible facade ────────────────────────────────
    #
    # The execute-phase worker calls ``create_per_task(task_id, sparse_paths=...)``
    # and ``remove_per_task(task_id)`` directly on the manager. The pool
    # exposes the same surface so the dispatcher can substitute a pool
    # for a manager without touching the worker. ``sparse_paths`` is
    # accepted but ignored at claim time — pool worktrees are full
    # checkouts (the cold-start budget assumes upfront cost is paid
    # once and recycled). Tests requiring sparse-checkout should keep
    # ``worktree_pool_enabled=False`` so the legacy lazy-create path
    # runs.

    async def create_per_task(
        self,
        task_id: str,
        base_ref: str = "HEAD",
        sparse_paths: list[str] | None = None,
    ) -> Path:
        """:class:`WorktreeManager`-compatible facade — delegates to claim().

        ``base_ref`` and ``sparse_paths`` are accepted for API parity but
        not honored: the pool returns a recycled worktree at the cold-
        start baseline, which makes ``base_ref`` and sparse-paths
        meaningless after the first cold-start. Operators who require
        sparse-checkout per task should disable the pool so the legacy
        lazy-create path runs.
        """
        del base_ref, sparse_paths  # API-parity only; not used by pool
        return await self.claim(task_id=task_id)

    async def remove_per_task(self, task_id: str, force: bool = True) -> None:
        """:class:`WorktreeManager`-compatible facade — delegates to release().

        C4: locates the worktree via the explicit reverse index
        :attr:`_task_to_path` (O(1) direct lookup) under ``_claim_lock``,
        instead of the previous O(n) reverse scan of :attr:`_claim_task` —
        the scan let two concurrent corrective tasks race so that the second
        claim's overwrite made the first task's lookup find the wrong (or no)
        path, silently leaking a worktree or mis-assigning one.

        If the task isn't tracked (e.g. the worker raised before claim, or a
        concurrent claim already stole/overwrote the slot), this logs a
        structured ``worktree_pool.remove_per_task_not_found`` warning and
        returns WITHOUT raising — raising here in production could mask real
        cleanup or crash the dispatcher mid-fan-out.
        """
        del force
        # Snapshot the reverse index under the lock so we read a consistent
        # task→path pairing even while peers are claiming/releasing.
        async with self._claim_lock:
            path_str = self._task_to_path.get(task_id)
        if path_str is None:
            async with self._claim_lock:
                tracked = sorted(self._task_to_path.keys())
            self._log.warning(
                "worktree_pool.remove_per_task_not_found",
                task_id=task_id,
                tracked=tracked,
            )
            return
        await self.release(Path(path_str), task_id=task_id)

    async def get_diff_vs_base(
        self, worktree: Path, base_ref: str = "HEAD"
    ) -> str:
        """Forward to the underlying :class:`WorktreeManager`.

        The execute-phase worker calls ``get_diff_vs_base`` to extract
        a unified diff from the per-task worktree before applying it
        to main. The pool delegates to its inner manager so the diff
        machinery is identical to the lazy-create path.
        """
        # Reset baseline is already at cold-start SHA; ``base_ref`` is
        # threaded through for API parity but the worker passes "HEAD"
        # which is correct against pool-baseline since we never advance
        # HEAD inside a pool worktree.
        return await self._mgr.get_diff_vs_base(worktree, base_ref=base_ref)

    async def apply_patch_to_main(
        self,
        worktree: Path,
        base_ref: str = "HEAD",
        three_way: bool = False,
        edit_scope: list[str] | None = None,
        commit_message: str | None = None,
    ) -> None:
        """Forward to the underlying :class:`WorktreeManager`."""
        await self._mgr.apply_patch_to_main(
            worktree,
            base_ref=base_ref,
            three_way=three_way,
            edit_scope=edit_scope,
            commit_message=commit_message,
        )

    async def expand_sparse_paths(
        self, worktree: Path, additional_paths: list[str]
    ) -> None:
        """Forward to the underlying :class:`WorktreeManager`.

        v0.20.0 C3 dynamic sparse-path expansion. No-op for pool
        worktrees (which are full checkouts) but the underlying
        manager handles that gracefully.
        """
        await self._mgr.expand_sparse_paths(worktree, additional_paths)

    async def cleanup_all(self) -> None:
        """Remove every worktree (pooled + overflow) and the pool dir.

        Safe to call multiple times. Errors are logged and swallowed —
        cleanup is best-effort.
        """
        # Drain any queued worktrees so claim() after cleanup doesn't
        # return a stale path.
        if self._available is not None:
            while not self._available.empty():
                try:
                    self._available.get_nowait()
                except asyncio.QueueEmpty:
                    break

        # Remove every label we created. WorktreeManager.cleanup_all
        # walks the directory and removes everything; we use it for
        # the broad sweep and rely on its idempotence.
        try:
            await self._mgr.cleanup_all()
        except Exception as exc:  # noqa: BLE001
            self._log.warning(
                "worktree_pool.cleanup_all.mgr_failed", err=str(exc)
            )

        # Final filesystem sweep — pool_dir may linger if the manager's
        # cleanup left detritus.
        if self._pool_dir.exists():
            try:
                shutil.rmtree(self._pool_dir, ignore_errors=True)
            except OSError:
                pass

        self._claimed.clear()
        self._claim_task.clear()
        self._task_to_path.clear()
        self._all_labels.clear()
        self._log.info("worktree_pool.cleanup_all.done")

    # ── Internal helpers ──────────────────────────────────────────────────

    def _record_claim(
        self, path: Path, label: str, task_id: str | None
    ) -> None:
        """Record a claim into all three maps. Caller MUST hold ``_claim_lock``.

        Writes ``_claimed`` (path→label) plus, when ``task_id`` is given, the
        forward ``_claim_task`` (path→task_id) and reverse ``_task_to_path``
        (task_id→path) indexes together so they never diverge. Pins the FULL
        task_id per [[explicit-identity]].
        """
        path_str = str(path)
        self._claimed[path_str] = label
        if task_id is not None:
            self._claim_task[path_str] = task_id
            self._task_to_path[task_id] = path_str

    def _label_for(self, path: Path) -> str:
        """Map a pool worktree path back to its label."""
        # Pool worktrees live at pool_dir/<label>; return the dir name.
        try:
            return path.relative_to(self._pool_dir).parts[0]
        except (ValueError, IndexError):
            return path.name

    async def _capture_baseline(self) -> str:
        """Return the main repo's HEAD SHA, or empty string on failure."""
        rc, out, _ = await _run_git(self._main, ["rev-parse", "HEAD"])
        if rc != 0:
            self._log.warning(
                "worktree_pool.baseline_capture_failed", rc=rc
            )
            return ""
        return out.strip()


__all__ = ["WorktreePool"]
