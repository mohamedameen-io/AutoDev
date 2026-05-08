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


# Terminal statuses for depends_on satisfaction checks (v0.11.0). Mirrors
# the constant in :mod:`orchestrator.execute_phase` but kept local to
# avoid a state→orchestrator import cycle.
_TERMINAL_TASK_STATUSES: frozenset[str] = frozenset(
    {"complete", "blocked", "skipped"}
)


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
        # v0.11.0: in-memory set of task ids currently being executed by
        # a worker. NOT persisted — by design. A crash mid-flight leaves
        # the underlying tasks in their pre-flight ``pending`` /
        # ``in_progress`` status; the resume path picks them up cleanly.
        self._in_flight: set[str] = set()

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
        """Return the first task with status ``pending`` (phase-major order).

        Backward-compat shim around :meth:`next_pending_tasks` for
        callers that still expect a single optional task. New callers
        in v0.11.0+ should use :meth:`next_pending_tasks` directly when
        running multiple workers concurrently.
        """
        tasks = await self.next_pending_tasks(limit=1)
        return tasks[0] if tasks else None

    async def next_pending_tasks(
        self,
        limit: int = 1,
        exclude_files: set[str] | None = None,
    ) -> list[Task]:
        """Return up to ``limit`` runnable pending tasks in phase-major order.

        v0.11.0: replaces the serial ``next_pending_task`` walk with a
        DAG-aware multi-task selector. A task is "runnable" iff:

        * ``status == "pending"``
        * every id in ``depends_on`` resolves to a task whose status is
          terminal (``"complete" | "blocked" | "skipped"``) — pending or
          in-flight deps make the task wait
        * no ``files`` entry intersects ``exclude_files`` — the
          dispatcher passes the union of in-flight task files to defer
          would-be conflicts upfront

        Walks phases sequentially (phase-major scheduling); within a
        phase, walks tasks in declaration order so the dispatcher's
        emission is stable and reproducible across runs given the same
        plan. Stops as soon as ``limit`` tasks are collected.

        ``exclude_files=None`` is treated as the empty set. ``limit < 1``
        is normalized to 1 (the dispatcher should clamp before calling).
        """
        if limit < 1:
            limit = 1
        excluded = exclude_files or set()

        async with plan_lock(self._cwd, timeout_s=self._lock_timeout_s):
            plan = self._load_sync()
            if plan is None:
                return []

            # Build a phase-major status map for cheap depends_on checks.
            status_by_id: dict[str, str] = {}
            for phase in plan.phases:
                for task in phase.tasks:
                    status_by_id[task.id] = task.status

            # Accumulator: every task we pick adds its files to the
            # running excluded set so subsequent picks in this batch
            # never share files with an earlier pick. Without this,
            # two batchmates with overlapping files would both ship.
            running_excluded: set[str] = set(excluded)
            picked: list[Task] = []
            for phase in plan.phases:
                for task in phase.tasks:
                    if len(picked) >= limit:
                        return picked
                    if task.status != "pending":
                        continue
                    # depends_on: every dep must be terminal.
                    deps_ok = True
                    for dep in task.depends_on:
                        dep_status = status_by_id.get(dep)
                        if dep_status not in _TERMINAL_TASK_STATUSES:
                            deps_ok = False
                            break
                    if not deps_ok:
                        continue
                    # exclude_files: must not intersect either the
                    # caller-provided set OR any earlier pick in this
                    # batch. The latter is the within-batch overlap
                    # guard — without it, two simultaneous picks could
                    # both touch the same file.
                    if running_excluded and any(
                        f in running_excluded for f in task.files
                    ):
                        continue
                    picked.append(task)
                    running_excluded.update(task.files)
            return picked

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

    # --- v0.11.0: in-flight tracking ---------------------------------

    async def mark_in_flight(self, task_id: str) -> None:
        """Record that a worker has started executing ``task_id``.

        In-memory only — persisted as an audit-only ledger op
        (``mark_in_flight``) for forensics. The set is reset on
        :class:`PlanManager` construction; resumes do NOT recover the
        live in-flight set.
        """
        async with plan_lock(self._cwd, timeout_s=self._lock_timeout_s):
            self._in_flight.add(task_id)
            await append_entry(
                self._cwd,
                op="mark_in_flight",
                payload={"task_id": task_id},
                session_id=self._session_id,
            )

    async def clear_in_flight(self, task_id: str) -> None:
        """Remove ``task_id`` from the in-flight set (idempotent)."""
        async with plan_lock(self._cwd, timeout_s=self._lock_timeout_s):
            self._in_flight.discard(task_id)
            await append_entry(
                self._cwd,
                op="clear_in_flight",
                payload={"task_id": task_id},
                session_id=self._session_id,
            )

    async def phase_in_flight_count(self, phase_id: str) -> int:
        """Return the count of in-flight tasks belonging to ``phase_id``.

        Used by the phase-review trigger to defer firing until every
        worker for the phase has finished. The plan_lock is held only
        long enough to read a consistent snapshot — the count is point-
        in-time so callers must re-check after they observe other state
        changes (double-checked locking).
        """
        async with plan_lock(self._cwd, timeout_s=self._lock_timeout_s):
            plan = self._load_sync()
            if plan is None:
                return 0
            phase_task_ids: set[str] = set()
            for phase in plan.phases:
                if phase.id == phase_id:
                    phase_task_ids = {t.id for t in phase.tasks}
                    break
            return sum(1 for tid in self._in_flight if tid in phase_task_ids)

    async def in_flight_files(self) -> set[str]:
        """Return the union of ``Task.files`` for all in-flight tasks.

        Passed to :meth:`next_pending_tasks` as ``exclude_files`` so the
        dispatcher refuses to start a new task whose files overlap any
        currently-executing task.
        """
        async with plan_lock(self._cwd, timeout_s=self._lock_timeout_s):
            plan = self._load_sync()
            if plan is None:
                return set()
            files: set[str] = set()
            in_flight = set(self._in_flight)
            for phase in plan.phases:
                for task in phase.tasks:
                    if task.id in in_flight:
                        files.update(task.files)
            return files

    async def mark_blocked_descendants(
        self,
        phase_id: str,
        failed_task_id: str,
        reason: str,
    ) -> list[str]:
        """Cascade-block every pending descendant of ``failed_task_id``.

        Walks reverse ``depends_on`` edges via :func:`orchestrator.dag.
        find_blocked_descendants` and transitions each pending one to
        ``"blocked"`` with ``blocked_reason="upstream-failure:{failed}:
        {reason}"``. Single ledger op (``mark_blocked_descendants``)
        carries the full id list so replay reproduces the cascade
        atomically — no half-applied state.

        Returns the list of task ids that were actually transitioned
        (i.e. were ``pending`` before the call). Already-terminal
        descendants are left alone so this method is safe to call
        multiple times for the same failed task.
        """
        from orchestrator.dag import find_blocked_descendants

        async with plan_lock(self._cwd, timeout_s=self._lock_timeout_s):
            plan = self._load_sync()
            if plan is None:
                return []
            phase = next((p for p in plan.phases if p.id == phase_id), None)
            if phase is None:
                return []
            descendants = find_blocked_descendants(phase, {failed_task_id})
            blocked_ids: list[str] = []
            for t in descendants:
                if t.status == "pending":
                    blocked_ids.append(t.id)
            if not blocked_ids:
                return []

            # Apply in-memory.
            structured_reason = f"upstream-failure:{failed_task_id}:{reason}"
            for t in phase.tasks:
                if t.id in blocked_ids:
                    t.status = "blocked"  # type: ignore[assignment]
                    t.blocked_reason = structured_reason

            # Single ledger op + snapshot.
            await append_entry(
                self._cwd,
                op="mark_blocked_descendants",
                payload={
                    "phase_id": phase_id,
                    "failed_task_id": failed_task_id,
                    "reason": reason,
                    "blocked_task_ids": blocked_ids,
                },
                session_id=self._session_id,
            )
            plan = plan.model_copy(update={"updated_at": _iso_now()})
            await snapshot_plan(self._cwd, plan, session_id=self._session_id)
            self._log.info(
                "task.cascade_blocked",
                phase_id=phase_id,
                failed_task_id=failed_task_id,
                blocked_count=len(blocked_ids),
            )
            return blocked_ids

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

    # --- v0.9.0: phase-level mutations -------------------------------

    async def append_corrective_tasks(
        self,
        phase_id: str,
        tasks: list[Task],
        review_status: str = "corrective_required",
    ) -> Plan:
        """Append corrective sub-tasks to ``phase_id`` and update review_status.

        Mirrors :meth:`update_task_status`'s lock + ledger + snapshot
        pattern. The phase's ``corrective_task_ids`` and ``review_status``
        are updated atomically alongside the ``tasks`` list. Idempotent on
        replay (see :func:`state.ledger._apply_op` for the
        ``append_corrective_tasks`` op).

        Returns the updated plan.
        """
        async with plan_lock(self._cwd, timeout_s=self._lock_timeout_s):
            plan = self._load_sync()
            if plan is None:
                raise PlanConcurrentModificationError(
                    "no plan initialized; call init_plan first"
                )
            phase = next((p for p in plan.phases if p.id == phase_id), None)
            if phase is None:
                raise PlanConcurrentModificationError(
                    f"phase_id={phase_id!r} not found in current plan"
                )

            existing_ids = {t.id for t in phase.tasks}
            appended: list[Task] = []
            for t in tasks:
                if t.id in existing_ids:
                    continue
                phase.tasks.append(t)
                existing_ids.add(t.id)
                if t.id not in phase.corrective_task_ids:
                    phase.corrective_task_ids.append(t.id)
                appended.append(t)
            phase.review_status = review_status  # type: ignore[assignment]

            await append_entry(
                self._cwd,
                op="append_corrective_tasks",
                payload={
                    "phase_id": phase_id,
                    "tasks": [t.model_dump(mode="json") for t in appended],
                    "review_status": review_status,
                },
                session_id=self._session_id,
            )
            plan = plan.model_copy(update={"updated_at": _iso_now()})
            await snapshot_plan(self._cwd, plan, session_id=self._session_id)
            self._log.info(
                "phase.corrective_tasks_appended",
                phase_id=phase_id,
                appended=len(appended),
                review_status=review_status,
            )
            return plan

    async def update_phase_meta(
        self,
        phase_id: str,
        *,
        baseline_commit: str | None = None,
        review_status: str | None = None,
    ) -> Plan:
        """Update phase-level metadata fields (baseline_commit / review_status).

        Lock + ledger + snapshot. Either field can be ``None`` to leave
        unchanged; passing both updates them in one ledger entry. Mirrors
        :meth:`update_task_status` semantics so resumes / replays
        reproduce the metadata transitions exactly.
        """
        async with plan_lock(self._cwd, timeout_s=self._lock_timeout_s):
            plan = self._load_sync()
            if plan is None:
                raise PlanConcurrentModificationError(
                    "no plan initialized; call init_plan first"
                )
            phase = next((p for p in plan.phases if p.id == phase_id), None)
            if phase is None:
                raise PlanConcurrentModificationError(
                    f"phase_id={phase_id!r} not found in current plan"
                )

            payload: dict = {"phase_id": phase_id}
            if baseline_commit is not None:
                phase.baseline_commit = baseline_commit
                payload["baseline_commit"] = baseline_commit
            if review_status is not None:
                phase.review_status = review_status  # type: ignore[assignment]
                payload["review_status"] = review_status

            await append_entry(
                self._cwd,
                op="update_phase_meta",
                payload=payload,
                session_id=self._session_id,
            )
            plan = plan.model_copy(update={"updated_at": _iso_now()})
            await snapshot_plan(self._cwd, plan, session_id=self._session_id)
            self._log.info(
                "phase.meta_updated",
                phase_id=phase_id,
                baseline_commit=baseline_commit,
                review_status=review_status,
            )
            return plan

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

    if op == "phase_review_complete":
        # v0.9.0: audit-only breadcrumb appended after the phase-review
        # tournament completes. Plan mutations live in ``update_phase_meta``
        # / ``append_corrective_tasks``.
        return plan

    if op in ("mark_in_flight", "clear_in_flight"):
        # v0.11.0: in-flight breadcrumbs do not mutate plan state — the
        # set is in-memory and rebuilt on resume from scratch.
        return plan

    if op in (
        "multi_branch_plan_tournament_start",
        "multi_branch_meta_merge_complete",
        "multi_branch_plan_tournament_complete",
    ):
        # v0.12.0: multi-branch audit ops do not mutate plan state. The
        # individual branches' ``plan_tournament_complete`` ops handle
        # the per-branch breadcrumbs; these three are aggregate forensics.
        return plan

    if op == "mark_blocked_descendants":
        # v0.11.0: cascade-block. Walk descendants and set
        # status="blocked" with a structured reason.
        phase_id = payload.get("phase_id")
        failed_task_id = payload.get("failed_task_id")
        reason = payload.get("reason", "")
        blocked_ids = payload.get("blocked_task_ids") or []
        if not isinstance(phase_id, str) or not isinstance(failed_task_id, str):
            return plan
        phase = next((p for p in plan.phases if p.id == phase_id), None)
        if phase is None:
            return plan
        if not isinstance(blocked_ids, list):
            return plan
        for tid in blocked_ids:
            if not isinstance(tid, str):
                continue
            for t in phase.tasks:
                if t.id == tid:
                    t.status = "blocked"  # type: ignore[assignment]
                    t.blocked_reason = (
                        f"upstream-failure:{failed_task_id}:{reason}"
                    )
                    break
        return plan

    if op == "append_corrective_tasks":
        # v0.9.0: same logic as :func:`state.ledger._apply_op` but tolerant
        # to missing / mismatched data during a load-fast-path replay.
        from state.schemas import Task as _Task

        phase_id = payload.get("phase_id")
        raw_tasks = payload.get("tasks") or []
        if not isinstance(phase_id, str):
            return plan
        phase = next((p for p in plan.phases if p.id == phase_id), None)
        if phase is None:
            return plan
        existing = {t.id for t in phase.tasks}
        for raw in raw_tasks:
            try:
                t = _Task.model_validate(raw)
            except Exception:
                continue
            if t.id not in existing:
                phase.tasks.append(t)
                existing.add(t.id)
            if t.id not in phase.corrective_task_ids:
                phase.corrective_task_ids.append(t.id)
        new_status = payload.get("review_status")
        if isinstance(new_status, str):
            phase.review_status = new_status  # type: ignore[assignment]
        return plan

    if op == "update_phase_meta":
        # v0.9.0: idempotent phase-meta update.
        phase_id = payload.get("phase_id")
        if not isinstance(phase_id, str):
            return plan
        phase = next((p for p in plan.phases if p.id == phase_id), None)
        if phase is None:
            return plan
        if "baseline_commit" in payload:
            val = payload["baseline_commit"]
            phase.baseline_commit = val if isinstance(val, str) else None
        if "review_status" in payload:
            val = payload["review_status"]
            phase.review_status = val if isinstance(val, str) else None  # type: ignore[assignment]
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
