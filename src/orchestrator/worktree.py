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
from runtime.language_profile import EXTENSION_WEIGHTS


logger = get_logger(__name__)


# v0.34.0 B2: cap on how many sibling header paths the sparse-checkout
# expansion may add. When the expansion exceeds this ceiling we skip
# the union entirely — dense include trees can balloon a 5-file edit
# scope into thousands of paths, defeating the point of sparse mode.
WORKTREE_HEADER_EXPANSION_CAP: int = 500


# v0.34.0 B2: source extensions sourced from
# ``runtime.language_profile.EXTENSION_WEIGHTS`` so the C/C++ family
# stays in lockstep with the language-profile classifier (the canonical
# list of source-file extensions in the codebase).
_CPP_SOURCE_EXTS: frozenset[str] = frozenset(
    {
        ext
        for ext, (lang, _w) in EXTENSION_WEIGHTS.items()
        if lang in ("cpp", "c") and ext not in (".h", ".hpp", ".hh", ".hxx")
    }
    | {".m", ".mm"}
)
_CPP_HEADER_GLOBS: tuple[str, ...] = ("*.h", "*.hpp", "*.hh", "*.hxx")


def _sibling_header_paths(
    source_files: Iterable[Path],
    git_root: Path,
    language_profile: dict | None = None,
) -> set[str]:
    """v0.34.0 B2: return repo-relative C/C++ header siblings of *source_files*.

    For each path whose extension is a C/C++ source extension, enumerate
    files in the SAME directory matching the C/C++ header globs. Only
    tracked files (per ``git ls-files <dir>``) are returned so the result
    is a strict subset of what the worktree already knows about.

    ``language_profile`` is accepted for future per-language gating but
    is not consulted in v0.34.0 — the source-extension filter alone is
    the gate.
    """
    del language_profile  # reserved for future per-language gating
    out: set[str] = set()
    seen_dirs: set[Path] = set()
    for src in source_files:
        if src.suffix.lower() not in _CPP_SOURCE_EXTS:
            continue
        parent = src.parent
        if parent in seen_dirs:
            continue
        seen_dirs.add(parent)
        try:
            rel_dir = parent.relative_to(git_root)
        except ValueError:
            rel_dir = parent
        rel_dir_posix = rel_dir.as_posix()
        for glob in _CPP_HEADER_GLOBS:
            spec = (
                f"{rel_dir_posix}/{glob}" if rel_dir_posix not in ("", ".") else glob
            )
            try:
                completed = subprocess_run_ls_files(git_root, spec)
            except OSError:
                continue
            for line in completed.splitlines():
                line = line.strip()
                if line:
                    out.add(line)
    return out


def subprocess_run_ls_files(git_root: Path, pathspec: str) -> str:
    """v0.34.0 B2: thin synchronous ``git ls-files`` wrapper.

    Sync rather than async because the call site is the sparse-paths
    computation in :meth:`WorktreeManager.create_per_task`, which is
    already running inside an async function but does its sparse-path
    setup synchronously. ``OSError`` is allowed to propagate so the
    caller can fall back cleanly when git is unavailable.
    """
    import subprocess

    completed = subprocess.run(
        ["git", "-C", str(git_root), "ls-files", pathspec],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        return ""
    return completed.stdout


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
        default_sparse_paths: list[str] | None = None,
    ) -> None:
        """Initialize a worktree manager.

        v0.22.1 A3: ``huge_mode`` flag from ``runtime.repo_probe.is_huge``
        extends the ``git worktree add`` timeout from 60 s to
        ``huge_create_timeout_s`` (default 600 s). On Unity-scale repos
        (358K files, 3 GB) full-checkout worktree creation can take
        80-180 s; the legacy 60 s ceiling killed it. Full sparse-by-default
        lands in v0.23.0 C1.

        v0.40.0 (huge-repo Gap 3): ``default_sparse_paths`` is an optional
        repo-relative cone applied by :meth:`create` when the caller does
        NOT pass an explicit ``sparse_paths``. The impl-tournament engine
        (:class:`tournament.ImplTournament`) calls ``create(nonce,
        base_ref="HEAD")`` with no scope of its own — it has no access to
        the task's files — so it would otherwise do a FULL checkout that
        times out (and materializes a multi-GB phantom) on a huge LFS repo,
        even though the execute-phase ``create_per_task`` path is already
        sparse. The runner now threads the task's files/extended_scope in
        here so all worktree-creation paths share one huge-safe cone.
        ``None`` (the default) preserves legacy full-checkout behavior for
        small repos and any caller that doesn't opt in.
        """
        self._main = Path(main_repo)
        self._dir = Path(tournament_dir)
        self._huge_mode = bool(huge_mode)
        self._huge_create_timeout_s = float(huge_create_timeout_s)
        self._default_sparse_paths = (
            list(default_sparse_paths) if default_sparse_paths else None
        )
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
        # v0.34.0 B2: count of sibling headers folded into the most
        # recent sparse create_per_task. The async caller emits the
        # ``sparse_worktree_expanded`` ledger op based on this counter
        # so the ledger write stays at the orchestrator's PlanManager
        # site (the manager does not own ledger access).
        self.last_sparse_headers_added: int = 0

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

        # v0.40.0 (huge-repo Gap 3): fall back to the instance-level
        # ``default_sparse_paths`` cone when the caller passed no explicit
        # scope. The impl-tournament engine calls ``create(nonce,
        # base_ref="HEAD")`` with ``sparse_paths=None``; without this the
        # tournament worktree would full-checkout and time out on a huge
        # LFS repo. ``None`` on both → legacy full checkout (small repos).
        if sparse_paths is None and self._default_sparse_paths:
            sparse_paths = list(self._default_sparse_paths)

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
        *,
        include_headers_for_sparse: bool = True,
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

        v0.39.0 (huge-repo follow-up): both the sparse ``--no-checkout``
        and the non-sparse fallback ``git worktree add`` now pass
        ``timeout_s=self._create_timeout_s()`` — they previously used the
        ``_run_git`` 60s default, so the execute-phase worktree path
        ignored ``huge_create_timeout_s`` (600s) and timed out at 60s on
        Unity-scale repos even though :meth:`create` already honored it.
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
                timeout_s=self._create_timeout_s(),
            )
            if rc != 0:
                raise WorktreeError(
                    f"git worktree add --no-checkout failed (rc={rc}): "
                    f"{err.strip() or out.strip()}"
                )
            # v0.34.0 B2: union sibling C/C++ headers into the sparse
            # set so include-chain-aware QA gates (hallucination guard,
            # build_check) keep symbol-resolution context. Capped at
            # WORKTREE_HEADER_EXPANSION_CAP — dense include trees would
            # otherwise regress to a near-full checkout.
            effective_paths = list(sparse_paths or [])
            added_headers = 0
            if include_headers_for_sparse and effective_paths:
                source_files = [self._main / p for p in effective_paths]
                extra = _sibling_header_paths(
                    source_files, self._main, language_profile=None
                )
                new_paths = sorted(extra - set(effective_paths))
                if len(new_paths) > WORKTREE_HEADER_EXPANSION_CAP:
                    self._log.warning(
                        "worktree.sparse_header_expansion.capped",
                        task_id=task_id,
                        proposed=len(new_paths),
                        cap=WORKTREE_HEADER_EXPANSION_CAP,
                    )
                else:
                    effective_paths.extend(new_paths)
                    added_headers = len(new_paths)
            for cmd in (
                ["sparse-checkout", "init", "--cone"],
                ["sparse-checkout", "set", *effective_paths],
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
                paths=effective_paths,
            )
            self.last_sparse_headers_added = added_headers
            if added_headers:
                # v0.34.0 B2: telemetry breadcrumb for sparse header
                # expansion. The caller (execute_phase dispatcher) is
                # the one with PlanManager access for ledger writes;
                # the manager itself only logs the structured event.
                self._log.info(
                    "sparse_worktree_expanded",
                    task_id=task_id,
                    added_paths=added_headers,
                    mode="sibling_headers",
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
            timeout_s=self._create_timeout_s(),
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

        check_args = ["apply", "--check", "--whitespace=fix"]
        apply_args = ["apply", "--whitespace=fix"]
        if three_way:
            check_args.append("--3way")
            apply_args.append("--3way")
        if commit_message is not None:
            # v0.25.1 Bug #2: stage as we apply so the follow-on commit
            # captures exactly the diff's hunks (no risk of sweeping
            # unrelated dirty state via a later ``git add -A``).
            apply_args.append("--index")

        # v0.40.0 (huge-repo Gap 3): belt-and-suspenders stale-lock cleanup
        # before ANY main-repo index mutation. A ``git worktree add`` killed
        # after timing out on a huge LFS repo can leave a stale
        # ``.git/index.lock`` behind; the reset/checkout/apply below would
        # then fail with "Unable to create '.../index.lock': File exists",
        # cascading one timeout into every subsequent apply. The helper only
        # removes a lock that is BOTH old and not held by a live process, so
        # a genuinely concurrent git op is never disturbed (no-op when no
        # lock exists → safe on small repos).
        from adapters.git_utils import clear_stale_index_lock

        if clear_stale_index_lock(self._main / ".git"):
            self._log.warning(
                "worktree.stale_index_lock_cleared",
                git_dir=str(self._main / ".git"),
            )

        # Robust integration: drop any leftover uncommitted dirt on the
        # files this patch targets (e.g. partial hunks staged by a prior
        # whitespace-rejected apply) so the pre-flight check sees a clean
        # base for them. Scoped to the diff's target files only — never a
        # blanket reset — so unrelated working-tree state is untouched.
        # ``checkout --`` restores each target to HEAD, which already
        # includes any earlier task committed via commit-per-task.
        from adapters.git_utils import extract_files_from_diff as _xf_targets

        _targets = _xf_targets(diff_text)
        if _targets:
            await _run_git(self._main, ["reset", "--quiet", "--", *_targets])
            await _run_git(self._main, ["checkout", "--", *_targets])

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

    async def abort_failed_apply(
        self,
        targets: list[str] | None = None,
    ) -> None:
        """v0.41.0 (A3): restore the main repo to a clean tree after a
        failed apply / 3-way merge.

        :meth:`apply_patch_to_main` with ``three_way=True`` falls back to
        git's merge machinery; when the 3-way *also* fails, git can leave
        the main working tree in one of two dirty states:

        * an **in-progress merge** (``--3way`` left ``MERGE_HEAD`` /
          ``.git/rebase-apply`` behind, with conflict markers staged), or
        * a **partial apply** (some hunks landed, some staged) with no
          merge in progress.

        Without cleanup, those conflict markers / partial hunks bleed into
        the *next* task's per-task worktree (created at ``HEAD`` of the main
        repo), corrupting an otherwise-correct downstream diff. This helper
        guarantees a clean tree before the caller marks the task blocked.

        Strategy (idempotent — safe to call when already clean):

        1. If a merge / 3-way-apply is in progress (``MERGE_HEAD`` or a
           ``.git/rebase-apply`` directory exists) → ``git merge --abort``.
        2. Otherwise (or if the abort is a no-op) → ``git reset --hard HEAD``
           to drop staged/partial hunks, then ``git clean -fd`` to remove
           any untracked files the apply introduced. When ``targets`` is
           supplied the ``git clean`` is **scoped to those paths** so
           unrelated untracked working-tree state is never swept.

        ``targets`` are repo-relative paths (typically ``task.files``).
        ``None`` / empty falls back to a repo-wide ``git clean -fd`` of
        untracked files, which is still safe because ``reset --hard``
        already restored every tracked file to ``HEAD``.

        A1 (Finding #1): the repo-wide ``git clean`` ALWAYS excludes AutoDev's
        own state directory (``.autodev/``, normally untracked in the target
        repo). Without this exclude, a corrective task synthesized by
        ``corrective_parser.parse_corrective_direction`` — which carries
        ``files=[]`` — drove the repo-wide path (empty ``targets``) and the
        ``git clean -fd`` DELETED ``.autodev/plan-ledger.jsonl`` out from under
        the live ``PlanManager``. The next ``block_task`` →
        ``update_task_status("blocked")`` → ``_load_sync()`` then read an empty
        ledger and raised ``"no plan initialized; call init_plan first"`` — the
        field-observed worker_exception that silently killed delivery on the
        conflict→re_architect→corrective→block path. AutoDev's run-state is
        never a legitimate ``git clean`` target, so excluding it is always
        correct (the scoped path is unaffected — it only cleans ``targets``).

        Never raises: a cleanup failure here must not mask the underlying
        apply failure the caller is already handling. Errors are logged and
        swallowed.
        """
        git_dir = self._main / ".git"
        merge_in_progress = (git_dir / "MERGE_HEAD").exists() or (
            git_dir / "rebase-apply"
        ).is_dir()

        if merge_in_progress:
            # ``git merge --abort`` resets the index and working tree to the
            # pre-merge state, clearing conflict markers and MERGE_HEAD.
            rc, _, err = await _run_git(self._main, ["merge", "--abort"])
            if rc == 0:
                self._log.info("worktree.abort_failed_apply.merge_aborted")
                return
            # Fall through to the hard-reset path if the abort itself
            # failed (e.g. the merge state was already half-cleared).
            self._log.warning(
                "worktree.abort_failed_apply.merge_abort_failed",
                err=err.strip(),
            )

        # Drop any staged/partial hunks so tracked files match HEAD again.
        reset_rc, _, reset_err = await _run_git(
            self._main, ["reset", "--hard", "HEAD"]
        )
        if reset_rc != 0:
            self._log.warning(
                "worktree.abort_failed_apply.reset_failed",
                err=reset_err.strip(),
            )

        # Remove untracked files the partial apply may have created. Scope
        # to the attempted targets when known so unrelated untracked state
        # (e.g. a developer's scratch file elsewhere) is untouched.
        #
        # A1: ALWAYS exclude AutoDev's own state dir from the clean. On the
        # repo-wide path (empty ``targets`` — the corrective-task shape, since
        # ``parse_corrective_direction`` produces tasks with ``files=[]``) an
        # un-excluded ``git clean -fd`` deleted the untracked
        # ``.autodev/plan-ledger.jsonl``, wiping the live plan and surfacing as
        # the spurious ``"no plan initialized"`` block. The exclude is harmless
        # on the scoped path (which lists explicit ``targets`` and never
        # ``.autodev``) and on a repo where ``.autodev`` is somehow tracked
        # (``git clean`` ignores tracked files regardless).
        clean_args = ["clean", "-fd"]
        autodev_excludes = self._git_clean_autodev_excludes()
        for exclude in autodev_excludes:
            clean_args.extend(["-e", exclude])
        scoped = [t for t in (targets or []) if t]
        if scoped:
            clean_args.append("--")
            clean_args.extend(scoped)
        clean_rc, _, clean_err = await _run_git(self._main, clean_args)
        if clean_rc != 0:
            self._log.warning(
                "worktree.abort_failed_apply.clean_failed",
                err=clean_err.strip(),
            )
        else:
            self._log.info(
                "worktree.abort_failed_apply.cleaned",
                scoped=bool(scoped),
                autodev_protected=bool(autodev_excludes),
            )

    def _git_clean_autodev_excludes(self) -> list[str]:
        """Return ``git clean -e`` pathspec(s) protecting AutoDev's state dir.

        A1 safety net: the per-repo ``.autodev/`` directory (ledger, snapshot,
        evidence, tournaments) is normally UNTRACKED in the target repo, so a
        repo-wide ``git clean -fd`` would delete it — destroying the live plan
        mid-run. This computes the exclude pathspec relative to the main repo
        root. Returns the canonical ``.autodev`` (the standard layout) plus, if
        ``self._autodev_root`` lives under the main repo at a non-standard
        location, that relative path too. Empty only when the autodev root is
        OUTSIDE the repo (then ``git clean`` can't reach it anyway).

        The canonical ``AUTODEV_DIR`` exclude is deliberately BROAD (un-anchored,
        so ``git clean -e`` protects a ``.autodev/`` wherever it sits in the
        tree). Protecting AutoDev's live run-state is always the safe direction,
        so this is NOT narrowed to a root-anchored ``/.autodev`` — that would
        stop protecting non-standard ``_autodev_root`` layouts.
        """
        from state.paths import AUTODEV_DIR

        excludes: list[str] = [AUTODEV_DIR]
        try:
            rel = self._autodev_root.resolve().relative_to(self._main.resolve())
            rel_str = rel.as_posix()
            # ``rel_str == "."`` means the autodev root IS the main repo root;
            # appending ``-e .`` would be a meaningless (and confusing) no-op.
            if rel_str not in (".", "") and rel_str not in excludes:
                excludes.append(rel_str)
        except (ValueError, OSError):
            # autodev_root is outside the repo (or unresolvable) — the
            # canonical ``.autodev`` exclude is the only relevant guard.
            pass
        return excludes


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
