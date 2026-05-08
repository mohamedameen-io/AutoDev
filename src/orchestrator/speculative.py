"""v0.21.0 B2: speculative-execution rollback handler.

Speculative execution starts a CHILD task while its PARENT is still
in-flight, accepting the bet that the parent will succeed. When the
parent succeeds, the child's work is valid — no extra steps. When the
parent fails (or returns a result that invalidates the child's
assumptions), this module's rollback handler:

* resets the child's worktree to baseline (or releases back to the
  pool),
* re-queues the child task as pending,
* emits a ``speculative_rolled_back`` ledger op so replay reconstructs
  the timeline.

Only ONE speculative task is allowed per phase at a time (per the
v0.21.0 plan) so a chain of speculative failures can't compound.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from autologging import get_logger
from orchestrator.worktree import WorktreeError, _run_git


if TYPE_CHECKING:
    from orchestrator import Orchestrator
    from orchestrator.worktree import WorktreeManager
    from state.schemas import Task


logger = get_logger(__name__)


async def reset_speculative_worktree(
    worktree: Path,
    baseline_commit: str,
) -> None:
    """Reset a speculative worktree to ``baseline_commit`` and clean.

    Mirrors :meth:`WorktreePool.release` reset semantics so the rollback
    is idempotent and produces a worktree at the same SHA the pool
    was cold-started against.

    Errors are logged but not raised — rollback is best-effort.
    """
    if not worktree.exists():
        logger.warning(
            "speculative.reset.path_missing", path=str(worktree)
        )
        return
    if baseline_commit:
        rc, out, err = await _run_git(
            worktree, ["reset", "--hard", baseline_commit]
        )
        if rc != 0:
            logger.warning(
                "speculative.reset.git_reset_failed",
                rc=rc,
                err=(err or out).strip(),
                path=str(worktree),
            )
            return
    rc2, out2, err2 = await _run_git(worktree, ["clean", "-fdx"])
    if rc2 != 0:
        logger.warning(
            "speculative.reset.git_clean_failed",
            rc=rc2,
            err=(err2 or out2).strip(),
            path=str(worktree),
        )


async def rollback_speculative_task(
    orch: "Orchestrator",
    speculative_task: "Task",
    parent_task_id: str,
    reason: str,
    *,
    worktree: Path | None = None,
    worktree_mgr: "WorktreeManager | None" = None,
    baseline_commit: str = "",
) -> None:
    """v0.21.0 B2: roll back a speculative task after parent failure.

    Steps:
    1. Best-effort reset of the speculative worktree (or pool release).
    2. Re-queue the speculative task as ``"pending"`` via
       :meth:`PlanManager.update_task_status`.
    3. Append a ``speculative_rolled_back`` ledger op for forensics.

    Idempotent: re-running with an already-pending task produces a
    duplicate ledger entry but no double mutation.
    """
    # Best-effort worktree reset.
    if worktree is not None and baseline_commit:
        try:
            await reset_speculative_worktree(worktree, baseline_commit)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "speculative.rollback.reset_failed",
                task_id=speculative_task.id,
                err=str(exc),
            )
    # If a manager is supplied, also remove the per-task worktree.
    if worktree_mgr is not None:
        try:
            await worktree_mgr.remove_per_task(
                speculative_task.id, force=True
            )
        except WorktreeError:
            pass
        except Exception:  # noqa: BLE001
            pass

    # Re-queue the speculative task as pending. ``revert_task_to_pending``
    # bypasses the FSM ``assert_transition`` check (which would reject
    # the in_progress→pending edge) — speculative rollback is the one
    # legitimate caller for that bypass.
    try:
        await orch.plan_manager.revert_task_to_pending(
            speculative_task.id,
            reason=(
                f"speculative_rollback: parent={parent_task_id} "
                f"reason={reason}"
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "speculative.rollback.requeue_failed",
            task_id=speculative_task.id,
            err=str(exc),
        )

    # Audit-only breadcrumb.
    try:
        await orch.plan_manager.ledger_append(
            op="speculative_rolled_back",
            payload={
                "task_id": speculative_task.id,
                "parent_task_id": parent_task_id,
                "reason": reason,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "speculative.rollback.ledger_append_failed",
            task_id=speculative_task.id,
            err=str(exc),
        )

    logger.info(
        "speculative.rollback.complete",
        task_id=speculative_task.id,
        parent_task_id=parent_task_id,
        reason=reason,
    )


async def commit_speculative_task(
    orch: "Orchestrator",
    speculative_task_id: str,
    parent_task_id: str,
) -> None:
    """v0.21.0 B2: confirm a speculative task after parent success.

    No worktree mutation needed (the task already ran to terminal).
    Just emits a ``speculative_committed`` ledger entry for forensics.
    """
    try:
        await orch.plan_manager.ledger_append(
            op="speculative_committed",
            payload={
                "task_id": speculative_task_id,
                "parent_task_id": parent_task_id,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "speculative.commit.ledger_append_failed",
            task_id=speculative_task_id,
            err=str(exc),
        )
    logger.info(
        "speculative.commit.complete",
        task_id=speculative_task_id,
        parent_task_id=parent_task_id,
    )


__all__ = [
    "commit_speculative_task",
    "reset_speculative_worktree",
    "rollback_speculative_task",
]
