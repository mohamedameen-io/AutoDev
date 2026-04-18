"""Load / save / mutate Plan via the ledger.

MVP subset with core functionality. Skipped for now: staleness detection,
plan.md derivation, auto-migration. These are easy to add later without
breaking the current API.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

from errors import PlanConcurrentModificationError
from autologging import get_logger
from state.ledger import (
    LedgerEntry,
    _apply_op,
    append_entry,
    read_entries,
    snapshot_plan,
)
from state.lockfile import plan_lock
from state.paths import plan_path
from state.schemas import Plan, Task, TaskStatus

logger = get_logger(__name__)


def _iso_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


class PlanManager:
    """Stateful facade over the plan + ledger.

    Every mutating method acquires :func:`plan_lock` internally and appends
    exactly one audit entry to the ledger (plus, where appropriate, a
    snapshot). Callers should create one ``PlanManager`` per orchestrator
    session and pass it down.
    """

    def __init__(
        self, cwd: Path, session_id: str, lock_timeout_s: float = 30.0
    ) -> None:
        self._cwd = Path(cwd)
        self._session_id = session_id
        self._lock_timeout_s = lock_timeout_s
        self._log = get_logger(component="plan_manager", session_id=session_id)

    @property
    def cwd(self) -> Path:
        return self._cwd

    @property
    def session_id(self) -> str:
        return self._session_id

    # --- Read helpers ---------------------------------------------------

    async def load(self) -> Plan | None:
        """Return the current plan (snapshot-first, fallback to full replay).

        If ``plan.json`` exists AND the ledger's tail is a ``snapshot``
        referring to the same content, we trust ``plan.json``. Otherwise
        fall back to replaying the ledger from the last ``snapshot`` (or
        from ``init_plan`` if none).
        """
        async with plan_lock(self._cwd, timeout_s=self._lock_timeout_s):
            return self._load_sync()

    def _load_sync(self) -> Plan | None:
        entries = read_entries(self._cwd)
        if not entries:
            # Could still have a plan.json from a crashed init — ignore it,
            # the ledger is the source of truth.
            return None

        # Try the snapshot fast-path. Walk backwards to find the latest.
        last_snapshot_idx: int | None = None
        for i in range(len(entries) - 1, -1, -1):
            if entries[i].op == "snapshot":
                last_snapshot_idx = i
                break
        if last_snapshot_idx is not None:
            snap = entries[last_snapshot_idx]
            base_plan = Plan.model_validate(snap.payload["plan"])
            # Apply any subsequent entries on top.
            for later in entries[last_snapshot_idx + 1 :]:
                base_plan = _apply_for_load(base_plan, later)
            return base_plan

        # Full replay (no snapshot yet) — reuse the already-read entries to
        # avoid a second disk read inside replay_ledger().
        plan: Plan | None = None
        for entry in entries:
            plan = _apply_op(plan, entry)
        return plan

    async def init_plan(self, plan: Plan) -> Plan:
        """Initialize a fresh plan. Fails if a plan already exists."""
        async with plan_lock(self._cwd, timeout_s=self._lock_timeout_s):
            existing = self._load_sync()
            if existing is not None:
                raise PlanConcurrentModificationError(
                    "plan already initialized; call save() or update_task_status()"
                )
            stamped = plan.model_copy(
                update={
                    "updated_at": _iso_now(),
                    "created_at": plan.created_at or _iso_now(),
                }
            )
            payload = stamped.model_dump(mode="json")
            await append_entry(
                self._cwd,
                op="init_plan",
                payload={"plan": payload},
                session_id=self._session_id,
            )
            await snapshot_plan(self._cwd, stamped, session_id=self._session_id)
            self._log.info("plan.initialized", plan_id=stamped.plan_id)
            return stamped

    async def save(self, plan: Plan) -> Plan:
        """Overwrite the plan wholesale.

        Appends an ``update_plan`` entry then a ``snapshot``. Use this for
        architect revisions; for single-task status changes use
        :meth:`update_task_status`.
        """
        async with plan_lock(self._cwd, timeout_s=self._lock_timeout_s):
            stamped = plan.model_copy(update={"updated_at": _iso_now()})
            await append_entry(
                self._cwd,
                op="update_plan",
                payload={"plan": stamped.model_dump(mode="json")},
                session_id=self._session_id,
            )
            await snapshot_plan(self._cwd, stamped, session_id=self._session_id)
            self._log.info("plan.saved", plan_id=stamped.plan_id)
            return stamped

    # --- Task helpers ---------------------------------------------------

    async def get_task(self, task_id: str) -> Task | None:
        async with plan_lock(self._cwd, timeout_s=self._lock_timeout_s):
            plan = self._load_sync()
            if plan is None:
                return None
            return _find_task(plan, task_id)

    async def next_pending_task(self) -> Task | None:
        """Return the first task with status ``pending`` (phase-major order)."""
        async with plan_lock(self._cwd, timeout_s=self._lock_timeout_s):
            plan = self._load_sync()
            if plan is None:
                return None
            for phase in plan.phases:
                for task in phase.tasks:
                    if task.status == "pending":
                        return task
        return None

    async def update_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        meta: dict | None = None,
    ) -> Task:
        """Transition one task to ``status``. Appends one ledger entry.

        ``meta`` may include ``blocked_reason``, ``retry_count``,
        ``escalated``, or ``evidence_bundle`` — any provided keys are merged
        into the payload and applied to the task.
        """
        from orchestrator.task_state import assert_transition

        async with plan_lock(self._cwd, timeout_s=self._lock_timeout_s):
            plan = self._load_sync()
            if plan is None:
                raise PlanConcurrentModificationError(
                    "no plan initialized; call init_plan first"
                )
            task = _find_task(plan, task_id)
            if task is None:
                raise PlanConcurrentModificationError(
                    f"task_id={task_id!r} not found in current plan"
                )
            assert_transition(task.status, status)

            payload = {"task_id": task_id, "status": status}
            if meta:
                payload.update(meta)
            await append_entry(
                self._cwd,
                op="update_task_status",
                payload=payload,
                session_id=self._session_id,
            )

            # Apply in-memory and persist a snapshot so reloads are fast.
            task.status = status
            if meta:
                if "blocked_reason" in meta:
                    task.blocked_reason = meta["blocked_reason"]
                if "retry_count" in meta:
                    task.retry_count = int(meta["retry_count"])
                if "escalated" in meta:
                    task.escalated = bool(meta["escalated"])
                if "evidence_bundle" in meta:
                    task.evidence_bundle = meta["evidence_bundle"]
            plan = plan.model_copy(update={"updated_at": _iso_now()})
            await snapshot_plan(self._cwd, plan, session_id=self._session_id)
            self._log.info(
                "task.status_updated",
                task_id=task_id,
                status=status,
                retry=task.retry_count,
                escalated=task.escalated,
            )
            return task

    async def mark_task_retry(self, task_id: str) -> int:
        """Increment a task's ``retry_count``. Returns the new count.

        Does NOT change status — caller is responsible for transitioning
        back to ``in_progress`` (or escalating).
        """
        async with plan_lock(self._cwd, timeout_s=self._lock_timeout_s):
            plan = self._load_sync()
            if plan is None:
                raise PlanConcurrentModificationError("no plan")
            task = _find_task(plan, task_id)
            if task is None:
                raise PlanConcurrentModificationError(f"unknown task {task_id}")
            task.retry_count += 1
            new_count = task.retry_count
            await append_entry(
                self._cwd,
                op="update_task_status",
                payload={
                    "task_id": task_id,
                    "status": task.status,
                    "retry_count": new_count,
                },
                session_id=self._session_id,
            )
            await snapshot_plan(self._cwd, plan, session_id=self._session_id)
            return new_count

    async def mark_escalated(self, task_id: str) -> None:
        """Flag a task as escalated to ``critic_sounding_board``."""
        async with plan_lock(self._cwd, timeout_s=self._lock_timeout_s):
            plan = self._load_sync()
            if plan is None:
                raise PlanConcurrentModificationError("no plan")
            task = _find_task(plan, task_id)
            if task is None:
                raise PlanConcurrentModificationError(f"unknown task {task_id}")
            task.escalated = True
            await append_entry(
                self._cwd,
                op="update_task_status",
                payload={
                    "task_id": task_id,
                    "status": task.status,
                    "escalated": True,
                },
                session_id=self._session_id,
            )
            await snapshot_plan(self._cwd, plan, session_id=self._session_id)

    async def read_ledger(self) -> list[LedgerEntry]:
        """Convenience accessor for debugging / CLI `status`."""
        return read_entries(self._cwd)

    async def ledger_append(
        self,
        op: str,
        payload: dict | None = None,
    ) -> LedgerEntry:
        """Append an arbitrary audit-only entry to the ledger.

        Intended for events that do not mutate plan state directly (e.g.,
        ``plan_tournament_complete``). The caller must ensure the ``op``
        string is registered in :data:`state.ledger.LedgerOp` and
        handled in ``ledger._apply_op``/``plan_manager._apply_for_load``
        (even if the handler is a no-op).
        """
        from typing import cast

        from state.ledger import LedgerOp

        async with plan_lock(self._cwd, timeout_s=self._lock_timeout_s):
            return await append_entry(
                self._cwd,
                op=cast(LedgerOp, op),
                payload=payload or {},
                session_id=self._session_id,
            )


def _find_task(plan: Plan, task_id: str) -> Task | None:
    for phase in plan.phases:
        for task in phase.tasks:
            if task.id == task_id:
                return task
    return None


def _apply_for_load(plan: Plan, entry: LedgerEntry) -> Plan:
    """Apply a post-snapshot entry during the load fast-path.

    Kept separate from :func:`state.ledger._apply_op` because that
    function raises for missing tasks (corruption signal during replay);
    during load we want to tolerate idempotent replays.
    """
    op = entry.op
    payload = entry.payload

    if op in ("init_plan", "update_plan", "snapshot"):
        return Plan.model_validate(payload["plan"])

    if op == "update_task_status":
        task = _find_task(plan, payload.get("task_id", ""))
        if task is None:
            return plan
        status = payload.get("status")
        if isinstance(status, str):
            task.status = status  # type: ignore[assignment]
        if "blocked_reason" in payload:
            task.blocked_reason = payload["blocked_reason"]
        if "retry_count" in payload:
            task.retry_count = int(payload["retry_count"])
        if "escalated" in payload:
            task.escalated = bool(payload["escalated"])
        if "evidence_bundle" in payload:
            task.evidence_bundle = payload["evidence_bundle"]
        return plan

    if op == "mark_blocked":
        task = _find_task(plan, payload.get("task_id", ""))
        if task is not None:
            task.status = "blocked"
            task.blocked_reason = payload.get("reason")
        return plan

    if op == "mark_complete":
        task = _find_task(plan, payload.get("task_id", ""))
        if task is not None:
            task.status = "complete"
        return plan

    if op == "append_evidence":
        task = _find_task(plan, payload.get("task_id", ""))
        path = payload.get("path")
        if task is not None and isinstance(path, str):
            task.evidence_bundle = path
        return plan

    if op == "plan_tournament_complete":
        # Audit-only breadcrumb (see ledger._apply_op). No plan mutation.
        return plan

    if op == "impl_tournament_complete":
        # Audit-only breadcrumb. No plan state mutation.
        return plan

    return plan


# Convenience for CLI / tests.
def current_plan_path(cwd: Path) -> Path:
    """Return the expected on-disk plan.json path."""
    return plan_path(cwd)


def read_plan_json(cwd: Path) -> Plan | None:
    """Best-effort read of plan.json without touching the ledger.

    Returns ``None`` if the file is missing or invalid.
    """
    pp = plan_path(cwd)
    if not pp.exists():
        return None
    try:
        raw = pp.read_text(encoding="utf-8")
        return Plan.model_validate_json(raw)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


__all__ = [
    "PlanManager",
    "current_plan_path",
    "read_plan_json",
]
