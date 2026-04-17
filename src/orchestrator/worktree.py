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
import shutil
from pathlib import Path
from typing import Iterable

from errors import AutodevError
from autologging import get_logger


logger = get_logger(__name__)


class WorktreeError(AutodevError):
    """Any failure creating, removing, or diffing a worktree."""


class WorktreeManager:
    """Create / remove ``git worktree`` directories for one impl tournament.

    One ``WorktreeManager`` instance owns a single ``tournament_dir`` on
    disk. Worktrees are labeled (``"a"`` / ``"b"`` / ``"ab"``) and land at
    ``tournament_dir/<label>``.
    """

    def __init__(self, main_repo: Path, tournament_dir: Path) -> None:
        self._main = Path(main_repo)
        self._dir = Path(tournament_dir)
        self._log = get_logger(
            component="worktree",
            main_repo=str(self._main),
            tournament_dir=str(self._dir),
        )

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

    async def create(self, label: str, base_ref: str = "HEAD") -> Path:
        """Create a new git worktree at ``tournament_dir/<label>``.

        Uses ``git worktree add --detach <path> <base_ref>`` so the worktree
        is not associated with any branch (matches short-lived use — nothing
        to conflict on branch names across parallel tournaments).

        Returns the worktree path. Raises :class:`WorktreeError` on failure.
        """
        wt = self.worktree_path(label)
        self._dir.mkdir(parents=True, exist_ok=True)
        if wt.exists():
            raise WorktreeError(
                f"worktree path {wt} already exists; call remove() first"
            )
        rc, out, err = await _run_git(
            self._main,
            ["worktree", "add", "--detach", str(wt), base_ref],
        )
        if rc != 0:
            raise WorktreeError(
                f"git worktree add failed (rc={rc}): {err.strip() or out.strip()}"
            )
        self._log.info("worktree.created", label=label, path=str(wt))
        return wt

    async def remove(self, label: str, force: bool = False) -> None:
        """Remove a worktree and its on-disk directory.

        First attempts the clean ``git worktree remove`` path. If that fails
        (uncommitted edits, corruption) and ``force=True``, falls back to
        ``git worktree remove --force`` + filesystem ``shutil.rmtree`` and
        a final ``git worktree prune`` to clean stale metadata.
        """
        wt = self.worktree_path(label)
        if not wt.exists():
            # Best-effort prune so the admin DB is consistent if a previous
            # remove failed mid-way.
            await _run_git(self._main, ["worktree", "prune"])
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
                return
            # Force flag was already set — escalate to rmtree + prune.
            await self._force_remove(wt)
            return
        self._log.info("worktree.removed", label=label, path=str(wt))

    async def _force_remove(self, wt: Path) -> None:
        """Fallback cleanup when ``git worktree remove`` can't finish."""
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

        Safe to call twice — subsequent calls are no-ops.
        """
        if not self._dir.exists():
            return

        # List all label subdirs.
        labels = [p.name for p in self._dir.iterdir() if p.is_dir()]
        for lbl in labels:
            try:
                await self.remove(lbl, force=True)
            except WorktreeError as exc:
                # Swallow — we're in cleanup, best effort only.
                self._log.warning(
                    "worktree.cleanup_remove_failed", label=lbl, err=str(exc)
                )

        # Remove whatever is left on disk (history.json, pass_NN/, etc).
        try:
            shutil.rmtree(self._dir, ignore_errors=True)
        except OSError:
            pass
        # Final prune.
        await _run_git(self._main, ["worktree", "prune"])
        self._log.info("worktree.cleanup_complete")

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
        self, worktree: Path, base_ref: str = "HEAD"
    ) -> None:
        """Apply the worktree's diff to the main repo's working tree.

        Strategy: compute ``get_diff_vs_base(worktree)`` then pipe to
        ``git apply`` from the main repo. Raises :class:`WorktreeError` on
        any apply conflict so the caller can surface a helpful error rather
        than leave the main repo half-patched.
        """
        diff_text = await self.get_diff_vs_base(worktree, base_ref=base_ref)
        if not diff_text.strip():
            self._log.info("worktree.apply_patch.empty_diff")
            return

        # Pre-flight: ``git apply --check`` so we fail fast on conflicts.
        check_rc, _, check_err = await _run_git(
            self._main,
            ["apply", "--check"],
            stdin=diff_text,
        )
        if check_rc != 0:
            raise WorktreeError(
                "cannot apply tournament winner to main repo "
                f"(conflict in working tree?): {check_err.strip()}"
            )
        apply_rc, _, apply_err = await _run_git(
            self._main,
            ["apply"],
            stdin=diff_text,
        )
        if apply_rc != 0:
            raise WorktreeError(
                f"git apply failed (rc={apply_rc}): {apply_err.strip()}"
            )
        self._log.info(
            "worktree.apply_patch.success",
            diff_bytes=len(diff_text),
        )


# ── Helpers ─────────────────────────────────────────────────────────────


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


__all__ = ["WorktreeError", "WorktreeManager"]
