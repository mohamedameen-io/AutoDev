"""Load / save / mutate Plan via the ledger.

MVP subset with core functionality. Skipped for now: staleness detection,
plan.md derivation, auto-migration. These are easy to add later without
breaking the current API.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

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


if TYPE_CHECKING:
    from orchestrator.escalation_ladder import StuckState

logger = get_logger(__name__)


# Terminal statuses for depends_on satisfaction checks (v0.11.0). Mirrors
# the constant in :mod:`orchestrator.execute_phase` but kept local to
# avoid a state→orchestrator import cycle.
_TERMINAL_TASK_STATUSES: frozenset[str] = frozenset(
    {"complete", "blocked", "skipped"}
)


def _iso_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _backfill_block_reason_class(plan: Plan) -> Plan:
    """v0.29.0 Bug 6: backfill ``Task.block_reason_class`` on legacy plans.

    Pre-v0.29.0 ``plan.json`` snapshots have no ``block_reason_class``
    on blocked tasks (the field didn't exist). Pydantic's ``None``
    default keeps load itself non-fatal, but downstream consumers that
    branch on the typed enum (refuse-force-accept in Bug 3, etc.)
    would treat the field as ``None`` ambiguously. This shim runs the
    keyword classifier from :mod:`state.infra_patterns` on the legacy
    ``blocked_reason`` string to backfill a typed value.

    Pure / idempotent: only mutates tasks whose ``status == "blocked"``
    AND whose ``block_reason_class is None``. New v0.29.0+ blocks that
    stamp the field explicitly are left alone, so a second call is a
    no-op. Conservative default — when the heuristic doesn't match,
    classifies as ``"verdict"`` so a misclassification can't auto-
    resume a task the agent legitimately rejected.
    """
    from state.infra_patterns import classify_blocked_reason

    for phase in plan.phases:
        for task in phase.tasks:
            if task.status == "blocked" and task.block_reason_class is None:
                task.block_reason_class = classify_blocked_reason(
                    task.blocked_reason
                )
    return plan


@dataclass(frozen=True)
class RequeueResult:
    """Outcome of :meth:`PlanManager.requeue_tasks` (v0.28.0 Bug 8).

    Carries the post-mutation summary the CLI surfaces to the operator
    plus the raw lists tests inspect to assert idempotency.

    ``requeued_task_ids`` excludes inputs that were already ``pending``
    (the requeue is idempotent so a second call returns an empty list
    here even when the same ids were passed in).

    ``reset_phase_ids`` is the set of phases whose ``review_status`` was
    flipped from any non-``None`` value back to ``None`` so the
    phase-review tournament fires fresh on the next execute pass.
    """

    requeued_task_ids: list[str] = field(default_factory=list)
    reset_phase_ids: list[str] = field(default_factory=list)


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
        # v0.15.0: in-memory per-task stuck state for the escalation
        # ladder. Mirrors ``_in_flight``: NOT persisted to plan.json or
        # the ledger; a crash mid-flight resets to defaults. The
        # cross-run lessons memory holds the durable signal.
        self._stuck_state: dict[str, "StuckState"] = {}

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
            return _backfill_block_reason_class(base_plan)

        # Full replay (no snapshot yet) — reuse the already-read entries to
        # avoid a second disk read inside replay_ledger().
        plan: Plan | None = None
        for entry in entries:
            plan = _apply_op(plan, entry)
        if plan is None:
            return None
        return _backfill_block_reason_class(plan)

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

        v0.30.0 Bug 4: ``meta`` may also carry forensic-only
        ``api_error_status`` (int) and ``last_adapter_subtype`` (str)
        keys propagated from the orchestrator's most recent adapter
        result. They flow into the ledger payload verbatim via
        :py:meth:`dict.update` below but are NOT applied to any
        :class:`Task` field — the Task model has no slot for them and
        they exist purely so post-mortems can grep block-class
        ``update_task_status`` entries for the API status / subtype
        that triggered the block, without diving into
        ``.autodev/debug/*.txt`` traceback dumps.
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
            # v0.32.0 (Phase 5, Gap G): normalise a RecoveryHint model
                # instance into its dict payload before the ledger
                # serialises (the ledger writes JSON; pydantic models
                # are not JSON-encodable). Use ``by_alias=True`` so the
                # wire shape matches the ``"class"`` alias every reader
                # expects.
            if "recovery_hint" in payload:
                from state.schemas import RecoveryHint as _RecoveryHint

                hint_val = payload["recovery_hint"]
                if isinstance(hint_val, _RecoveryHint):
                    payload["recovery_hint"] = hint_val.model_dump(
                        by_alias=True
                    )
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
                if "last_retry_at" in meta:
                    last_retry_at = meta["last_retry_at"]
                    task.last_retry_at = (
                        str(last_retry_at) if last_retry_at is not None else None
                    )
                if "escalated" in meta:
                    task.escalated = bool(meta["escalated"])
                if "evidence_bundle" in meta:
                    task.evidence_bundle = meta["evidence_bundle"]
                # v0.29.0 Bug 6: typed block category. Validated by the
                # Pydantic Literal on :attr:`Task.block_reason_class`.
                if "block_reason_class" in meta:
                    cls = meta["block_reason_class"]
                    if cls not in (None, "verdict", "infrastructure", "cap"):
                        raise ValueError(
                            f"block_reason_class must be one of "
                            f"verdict|infrastructure|cap|None, got {cls!r}"
                        )
                    task.block_reason_class = cls
                # Step 5 (RECOVERY-CONTRACT §7 Part 3): persist the resolver's
                # ``resolver_note`` / ``resolver_action`` onto the Task model so
                # the guidance survives the status round-trip (a reload/replay).
                # Pre-Step-5 these landed ONLY in the ledger payload above and
                # were dropped from the in-memory Task + snapshot, so the
                # developer loop could never read the resolver's guidance back
                # into ``last_issues``. ``Task.metadata`` is a persisted field, so
                # routing them there makes ``_resolver_retry``'s note readable
                # after the transition (the RECOVER_TASK guidance-injection
                # channel from the contract). Merge (don't clobber) so other
                # metadata keys survive.
                #
                # WS6: ``model_override`` rides the same persisted channel. The
                # resolver's ``escalate_model`` recovery stamps a validated model
                # alias here; ``execute_phase.delegate`` reads it back on the next
                # dispatch so the escalated model actually takes effect (and
                # survives ``autodev resume``). A ``None`` value clears it (same
                # explicit-clear semantics as the resolver note).
                for _mkey in ("resolver_note", "resolver_action", "model_override"):
                    if _mkey not in meta:
                        continue
                    _mval = meta[_mkey]
                    new_md = dict(task.metadata or {})
                    if _mval is None:
                        # Explicit clear (the developer loop consumes the note
                        # after injecting it into last_issues).
                        new_md.pop(_mkey, None)
                    else:
                        new_md[_mkey] = str(_mval)
                    task.metadata = new_md
                # WS5: persist the best-effort-commit terminal markers onto the
                # Task model so the completed task is SELF-DESCRIBING for a
                # benchmark scorer (a non-``blocked`` terminal that is NOT
                # "solved"). Purely additive — these keys are only ever set by
                # the ``best_effort_commit`` ask_human path, so every existing
                # flow (and the default ``block`` mode) is byte-identical.
                # ``needs_human_review`` is stored verbatim (bool);
                # ``completion_reason`` is coerced to str. Merge, don't clobber.
                for _mkey, _coerce in (
                    ("needs_human_review", bool),
                    ("completion_reason", str),
                ):
                    if _mkey not in meta:
                        continue
                    new_md = dict(task.metadata or {})
                    new_md[_mkey] = _coerce(meta[_mkey])
                    task.metadata = new_md
                # v0.32.0 (Phase 5, Gap G): structured recovery hint.
                # Accept either a :class:`RecoveryHint` model instance OR
                # the equivalent ``dict`` payload (covers callers that
                # built the meta from JSON / persisted ledger ops).
                # ``None`` clears the field.
                if "recovery_hint" in meta:
                    from state.schemas import RecoveryHint as _RecoveryHint

                    raw_hint = meta["recovery_hint"]
                    if raw_hint is None:
                        task.recovery_hint = None
                    elif isinstance(raw_hint, _RecoveryHint):
                        task.recovery_hint = raw_hint
                    else:
                        task.recovery_hint = _RecoveryHint.model_validate(
                            raw_hint
                        )
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

    async def revert_task_to_pending(
        self,
        task_id: str,
        *,
        reason: str = "",
    ) -> Task:
        """v0.21.0 B2: forcibly revert a task back to ``"pending"``.

        Bypasses the FSM ``assert_transition`` check used by
        :meth:`update_task_status`. Used exclusively by the speculative-
        execution rollback path: when a speculative task started but
        its parent later failed, the speculative work is invalidated
        and the task must re-queue from scratch (running a fresh
        attempt against the parent's eventual successful state).

        Persists via the standard lock + ledger + snapshot pipeline
        and emits a regular ``update_task_status`` op so replay
        reconstructs the transition exactly. ``reason`` is recorded
        as ``blocked_reason`` for forensics.
        """
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

            payload: dict = {"task_id": task_id, "status": "pending"}
            if reason:
                payload["blocked_reason"] = reason

            await append_entry(
                self._cwd,
                op="update_task_status",
                payload=payload,
                session_id=self._session_id,
            )

            task.status = "pending"
            if reason:
                task.blocked_reason = reason
            # Reset retry / escalation bookkeeping — the next dispatch
            # treats this as a fresh attempt.
            task.retry_count = 0
            task.escalated = False

            plan = plan.model_copy(update={"updated_at": _iso_now()})
            await snapshot_plan(self._cwd, plan, session_id=self._session_id)
            self._log.info(
                "task.reverted_to_pending",
                task_id=task_id,
                reason=reason,
            )
            return task

    async def requeue_tasks(
        self,
        task_ids: list[str],
        *,
        reset_phase_review: bool = True,
        source: str = "interactive",
    ) -> RequeueResult:
        """v0.28.0 Bug 8: typed task-status reset for the ``requeue`` CLI.

        For every id in ``task_ids`` whose current status is NOT
        already ``pending``: flip status to ``pending``, zero
        ``retry_count``, clear ``escalated`` + ``blocked_reason``.
        Tasks already at ``pending`` are skipped (idempotent — a second
        call writes zero ledger entries for those tasks).

        When ``reset_phase_review`` is true (default), every phase
        containing at least one requeued task has its ``review_status``
        flipped back to ``None`` via the regular ``update_phase_meta``
        op, so the phase-review tournament re-fires fresh on the next
        execute pass instead of believing the phase is already
        accepted.

        ``source`` is a short label ("--task" | "--phase" |
        "--infrastructure" | "--all-blocked" | "interactive") recorded
        in the audit ``requeue`` ledger entry so forensics can later
        reconstruct *why* the operator triggered the requeue.

        Bypasses the FSM ``assert_transition`` check: ``blocked →
        pending`` is not a legal automatic edge (would trigger
        spontaneous retries), but it IS a legal operator-driven edge
        and the ledger captures the explicit ``op="requeue"`` audit
        breadcrumb so the transition is never silent.
        """
        async with plan_lock(self._cwd, timeout_s=self._lock_timeout_s):
            plan = self._load_sync()
            if plan is None:
                raise PlanConcurrentModificationError(
                    "no plan initialized; call init_plan first"
                )

            # Pre-pass: validate every id resolves AND determine which
            # tasks actually need a transition. Anything already
            # pending is filtered out so the call is idempotent.
            tasks_to_requeue: list[Task] = []
            for tid in task_ids:
                task = _find_task(plan, tid)
                if task is None:
                    raise PlanConcurrentModificationError(
                        f"task_id={tid!r} not found in current plan"
                    )
                if task.status == "pending":
                    continue
                tasks_to_requeue.append(task)

            if not tasks_to_requeue:
                # Idempotent no-op — no audit breadcrumb either, so
                # re-running ``autodev requeue --task X`` after success
                # writes zero ledger entries.
                return RequeueResult()

            requeued_ids = [t.id for t in tasks_to_requeue]

            # Audit-only breadcrumb capturing CLI intent. Emitted
            # BEFORE the per-task ops so a partial-write crash leaves
            # the breadcrumb but no half-applied state — replay then
            # reconstructs whichever subset of update_task_status ops
            # actually landed.
            await append_entry(
                self._cwd,
                op="requeue",
                payload={
                    "task_ids": requeued_ids,
                    "reset_phase_review": bool(reset_phase_review),
                    "source": source,
                },
                session_id=self._session_id,
            )

            # Per-task transitions. Each emits its own
            # ``update_task_status`` op so replay reproduces the full
            # mutation set even if the audit breadcrumb were ever lost.
            for task in tasks_to_requeue:
                await append_entry(
                    self._cwd,
                    op="update_task_status",
                    payload={
                        "task_id": task.id,
                        "status": "pending",
                        "blocked_reason": None,
                        "retry_count": 0,
                        "escalated": False,
                    },
                    session_id=self._session_id,
                )
                task.status = "pending"
                task.blocked_reason = None
                task.retry_count = 0
                task.escalated = False

            # Phase-level review reset. Only phases that actually held
            # a non-None review_status get touched — re-running on a
            # fresh phase would otherwise emit a redundant noop entry.
            reset_phase_ids: list[str] = []
            if reset_phase_review:
                affected_phase_ids = {t.phase_id for t in tasks_to_requeue}
                for phase in plan.phases:
                    if phase.id not in affected_phase_ids:
                        continue
                    if phase.review_status is None:
                        continue
                    await append_entry(
                        self._cwd,
                        op="update_phase_meta",
                        payload={
                            "phase_id": phase.id,
                            "review_status": None,
                        },
                        session_id=self._session_id,
                    )
                    phase.review_status = None
                    reset_phase_ids.append(phase.id)

            plan = plan.model_copy(update={"updated_at": _iso_now()})
            await snapshot_plan(self._cwd, plan, session_id=self._session_id)
            self._log.info(
                "requeue.applied",
                task_ids=requeued_ids,
                reset_phase_ids=reset_phase_ids,
                source=source,
            )
            return RequeueResult(
                requeued_task_ids=requeued_ids,
                reset_phase_ids=reset_phase_ids,
            )

    async def reconcile_evidence_vs_ledger(self) -> dict[str, list]:
        """v0.22.2 B3: detect + repair orphan evidence at resume time.

        Walks ``.autodev/evidence/*-developer.json`` files. For each that
        reports ``success=true``, checks whether a matching ``coded`` (or
        higher) ``update_task_status`` op exists in the ledger. If not,
        AND an ``attempt_started`` marker exists for the same task, AND
        the task's current status is still ``pending``/``in_progress``,
        promote: emit a fresh ``update_task_status(coded)`` op carrying
        the original evidence file's mtime in ``meta``. Discrepancies
        (no marker, terminal status, etc.) are collected for operator
        review and emitted as a single ``reconcile_evidence`` audit op.

        Idempotent: re-running with no orphans is a no-op (and does NOT
        emit the summary op when both lists are empty).

        D-3's finding from the 2026-05-09 Unity stall: ``write_evidence``
        at ``execute_phase.py:1771`` runs BEFORE the
        ``update_task_status(coded)`` at ``:1818``. A crash in between
        leaves the evidence on disk but no ledger record — recovery
        then resets the task to ``pending`` and re-runs from scratch,
        discarding the completed work.

        Returns:
            ``{"promoted": [...], "discrepancies": [...]}``.
        """
        from state.evidence import list_evidence
        from state.ledger import append_entry, read_entries
        from state.paths import evidence_dir
        from state.schemas import CoderEvidence

        plan = await self.load()
        if plan is None:
            return {"promoted": [], "discrepancies": []}

        entries = read_entries(self._cwd)
        attempts_started: set[str] = set()
        coded_seen: set[str] = set()
        for e in entries:
            if e.op == "attempt_started":
                tid = e.payload.get("task_id")
                if isinstance(tid, str):
                    attempts_started.add(tid)
            elif e.op == "update_task_status":
                tid = e.payload.get("task_id")
                st = e.payload.get("status")
                if isinstance(tid, str) and st in (
                    "coded",
                    "auto_gated",
                    "reviewed",
                    "tested",
                    "tournamented",
                    "complete",
                ):
                    coded_seen.add(tid)

        promoted: list[str] = []
        discrepancies: list[dict] = []

        d = evidence_dir(self._cwd)
        if not d.exists():
            return {"promoted": [], "discrepancies": []}

        for p in sorted(d.iterdir()):
            if not (p.is_file() and p.name.endswith("-developer.json")):
                continue
            task_id = p.name[: -len("-developer.json")]
            if task_id in coded_seen:
                continue
            try:
                evs = await list_evidence(self._cwd, task_id)
            except Exception:  # noqa: BLE001
                continue
            coder_ev = next(
                (e for e in evs if isinstance(e, CoderEvidence)), None
            )
            if coder_ev is None or not getattr(coder_ev, "success", False):
                continue
            task = _find_task(plan, task_id)
            if task is None:
                discrepancies.append(
                    {"task_id": task_id, "reason": "evidence_orphan_no_task"}
                )
                continue
            if task_id not in attempts_started:
                discrepancies.append(
                    {
                        "task_id": task_id,
                        "reason": "evidence_without_attempt_started_marker",
                    }
                )
                continue
            if task.status not in ("pending", "in_progress"):
                discrepancies.append(
                    {
                        "task_id": task_id,
                        "reason": f"task_terminal_status={task.status}",
                    }
                )
                continue
            try:
                from datetime import datetime as _datetime, timezone as _tz

                mtime_iso = _datetime.fromtimestamp(
                    p.stat().st_mtime, tz=_tz.utc
                ).isoformat()
                # FSM transitions are pending → in_progress → coded; promote
                # in two steps so ``assert_transition`` passes both edges.
                if task.status == "pending":
                    await self.update_task_status(task_id, "in_progress")
                await self.update_task_status(
                    task_id,
                    "coded",
                    meta={
                        "evidence_bundle": str(p),
                        "reconciled_from_evidence_mtime": mtime_iso,
                    },
                )
                promoted.append(task_id)
            except Exception as exc:  # noqa: BLE001
                discrepancies.append(
                    {"task_id": task_id, "reason": f"promote_failed: {exc}"}
                )

        if promoted or discrepancies:
            async with plan_lock(self._cwd, timeout_s=self._lock_timeout_s):
                await append_entry(
                    self._cwd,
                    op="reconcile_evidence",
                    payload={
                        "promoted": promoted,
                        "discrepancies": discrepancies,
                    },
                    session_id=self._session_id,
                )
        if promoted or discrepancies:
            self._log.info(
                "reconcile_evidence.complete",
                promoted=len(promoted),
                discrepancies=len(discrepancies),
            )
        return {"promoted": promoted, "discrepancies": discrepancies}

    async def reap_orphans(
        self,
        *,
        reason: str = "orphan_reaped_on_resume",
    ) -> list[str]:
        """v0.22.2 B1: revert any non-terminal-non-pending task to ``pending``.

        Walks the plan and identifies tasks wedged in
        ``{"in_progress", "coded", "auto_gated", "reviewed", "tested",
        "tournamented"}`` — states a healthy run never persists across
        process boundaries. Calls :meth:`revert_task_to_pending` on each.
        Idempotent (safe to call multiple times — second call is a no-op).

        D-2 (the 2026-05-09 Unity stall investigation) showed that an
        interrupted run leaves tasks frozen in non-terminal states, and
        the dispatcher's ``next_pending_tasks`` filters on
        ``status=="pending"`` only — wedged tasks were unrecoverable
        without manual ledger surgery. This sweeper closes the loop.

        Notes on lock ordering:
            ``revert_task_to_pending`` re-acquires ``plan_lock`` per
            call, so this method MUST NOT hold the lock around the
            per-task loop. We snapshot the orphan IDs first (under no
            lock — best-effort), then revert each.

        Returns:
            The list of reaped task IDs (in scan order).
        """
        plan = await self.load()
        if plan is None:
            return []
        orphan_ids: list[str] = []
        for phase in plan.phases:
            for t in phase.tasks:
                if (
                    t.status not in _TERMINAL_TASK_STATUSES
                    and t.status != "pending"
                ):
                    orphan_ids.append(t.id)
        for tid in orphan_ids:
            try:
                await self.revert_task_to_pending(tid, reason=reason)
            except PlanConcurrentModificationError:
                # Concurrent edit raced us — safe to skip; the next
                # caller will re-scan. Don't surface as fatal.
                self._log.warning(
                    "reap_orphans.skipped_concurrent_modification",
                    task_id=tid,
                )
        if orphan_ids:
            self._log.info(
                "reap_orphans.complete",
                count=len(orphan_ids),
                reason=reason,
            )
        return orphan_ids

    async def speculable_candidate(
        self,
        in_flight_task_id: str,
    ) -> Task | None:
        """v0.21.0 B2: return a child task safe to start speculatively.

        Returns a single :class:`Task` that depends on ``in_flight_task_id``
        and is eligible to run speculatively, or ``None`` when no
        candidate qualifies. Conditions checked here:

        * the parent task (``in_flight_task_id``) is itself in-flight
          (status ``"in_progress"`` or post-developer non-terminal),
        * the parent's ``retry_count == 0`` (first attempt only — if
          the parent is already on a retry, speculative work would
          compound risk),
        * the dependent task has a SINGLE ``depends_on`` entry pointing
          at the parent (not a multi-dep diamond — diamonds get
          materially more complex rollbacks),
        * the dependent's files are disjoint with EVERY currently in-
          flight task's files (file-overlap guard preserved),
        * the dependent is currently ``"pending"``.

        The dispatcher decides whether to actually start it, and how
        many speculative tasks to allow concurrently (cap of 1 per
        phase per the v0.21.0 plan).
        """
        async with plan_lock(self._cwd, timeout_s=self._lock_timeout_s):
            plan = self._load_sync()
            if plan is None:
                return None

            # Locate the parent task.
            parent: Task | None = _find_task(plan, in_flight_task_id)
            if parent is None:
                return None
            if parent.retry_count != 0:
                return None
            # Verify parent is in-flight (pre-terminal).
            if parent.status in _TERMINAL_TASK_STATUSES:
                return None

            # Build in-flight files set (excluding parent's files —
            # the speculative task can't share files with the parent
            # because the parent is in flight too, but it can share
            # files with a hypothetically-completed parent. Use the
            # actual in-flight set from PlanManager.)
            in_flight_ids = set(self._in_flight)
            in_flight_files: set[str] = set()
            for ph in plan.phases:
                for t in ph.tasks:
                    if t.id in in_flight_ids:
                        in_flight_files.update(t.files)

            # Walk all phases looking for a child of in_flight_task_id.
            for ph in plan.phases:
                for t in ph.tasks:
                    if t.status != "pending":
                        continue
                    # Single parent only (diamond avoidance).
                    if t.depends_on != [in_flight_task_id]:
                        continue
                    # File-disjointness with every in-flight task.
                    if any(f in in_flight_files for f in t.files):
                        continue
                    return t
            return None

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

        v0.29.0 Bug 6: every cascaded descendant inherits the parent's
        :attr:`Task.block_reason_class`. When the parent has no class
        (legacy plan path), the cascade defaults to ``"verdict"`` —
        the conservative pick (won't auto-resume on a future
        ``--infrastructure`` requeue).

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

            # Look up the parent's class so cascaded descendants
            # inherit it. Default to ``"verdict"`` when the parent is
            # absent or unstamped (conservative).
            parent_task = _find_task(plan, failed_task_id)
            inherited_class = (
                parent_task.block_reason_class
                if parent_task is not None
                and parent_task.block_reason_class is not None
                else "verdict"
            )

            # Apply in-memory.
            structured_reason = f"upstream-failure:{failed_task_id}:{reason}"
            for t in phase.tasks:
                if t.id in blocked_ids:
                    t.status = "blocked"  # type: ignore[assignment]
                    t.blocked_reason = structured_reason
                    t.block_reason_class = inherited_class

            # Single ledger op + snapshot.
            await append_entry(
                self._cwd,
                op="mark_blocked_descendants",
                payload={
                    "phase_id": phase_id,
                    "failed_task_id": failed_task_id,
                    "reason": reason,
                    "blocked_task_ids": blocked_ids,
                    "block_reason_class": inherited_class,
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

    # --- v0.15.0: stuck-state tracking (in-memory per task) -----------

    async def get_stuck_state(self, task_id: str) -> "StuckState":
        """Return the current :class:`StuckState` for ``task_id``.

        Returns a fresh zero-valued state when the task is unknown
        (mirrors the in-memory-only design — no ledger op fires).
        """
        from orchestrator.escalation_ladder import StuckState

        async with plan_lock(self._cwd, timeout_s=self._lock_timeout_s):
            existing = self._stuck_state.get(task_id)
            if existing is None:
                return StuckState()
            # Return a copy so callers can't accidentally mutate our state.
            return StuckState(
                discard_count=existing.discard_count,
                pivot_count=existing.pivot_count,
                search_count=existing.search_count,
                last_search_iter=existing.last_search_iter,
                architect_count=existing.architect_count,
                last_event=existing.last_event,
            )

    async def increment_discard(self, task_id: str) -> "StuckState":
        """Bump ``discard_count`` for ``task_id`` and return the updated state.

        Held under :func:`plan_lock` so concurrent workers cannot race on
        the same task. State is in-memory only — no ledger op is appended.
        """
        from orchestrator.escalation_ladder import StuckState

        async with plan_lock(self._cwd, timeout_s=self._lock_timeout_s):
            existing = self._stuck_state.get(task_id)
            if existing is None:
                existing = StuckState()
            updated = StuckState(
                discard_count=existing.discard_count + 1,
                pivot_count=existing.pivot_count,
                search_count=existing.search_count,
                last_search_iter=existing.last_search_iter,
                architect_count=existing.architect_count,
                last_event="discard",
            )
            self._stuck_state[task_id] = updated
            return StuckState(
                discard_count=updated.discard_count,
                pivot_count=updated.pivot_count,
                search_count=updated.search_count,
                last_search_iter=updated.last_search_iter,
                architect_count=updated.architect_count,
                last_event=updated.last_event,
            )

    async def increment_pivot(self, task_id: str) -> "StuckState":
        """Bump ``pivot_count`` for ``task_id`` and return the updated state.

        Held under :func:`plan_lock`. State is in-memory only.
        """
        from orchestrator.escalation_ladder import StuckState

        async with plan_lock(self._cwd, timeout_s=self._lock_timeout_s):
            existing = self._stuck_state.get(task_id)
            if existing is None:
                existing = StuckState()
            updated = StuckState(
                discard_count=existing.discard_count,
                pivot_count=existing.pivot_count + 1,
                search_count=existing.search_count,
                last_search_iter=existing.last_search_iter,
                architect_count=existing.architect_count,
                last_event="pivot",
            )
            self._stuck_state[task_id] = updated
            return StuckState(
                discard_count=updated.discard_count,
                pivot_count=updated.pivot_count,
                search_count=updated.search_count,
                last_search_iter=updated.last_search_iter,
                architect_count=updated.architect_count,
                last_event=updated.last_event,
            )

    async def increment_search(self, task_id: str) -> "StuckState":
        """v0.17.0 S2: bump ``search_count`` for ``task_id``.

        Mirrors :meth:`increment_discard` / :meth:`increment_pivot`.
        Used by the escalation ladder when a WEB_SEARCH rung fires; the
        counter caps autonomous searches at
        :data:`orchestrator.escalation_ladder._SEARCH_COOLDOWN_CAP`
        (3 per task).
        """
        from orchestrator.escalation_ladder import StuckState

        async with plan_lock(self._cwd, timeout_s=self._lock_timeout_s):
            existing = self._stuck_state.get(task_id)
            if existing is None:
                existing = StuckState()
            updated = StuckState(
                discard_count=existing.discard_count,
                pivot_count=existing.pivot_count,
                search_count=existing.search_count + 1,
                last_search_iter=existing.last_search_iter + 1,
                architect_count=existing.architect_count,
                last_event="web_search",
            )
            self._stuck_state[task_id] = updated
            return StuckState(
                discard_count=updated.discard_count,
                pivot_count=updated.pivot_count,
                search_count=updated.search_count,
                last_search_iter=updated.last_search_iter,
                architect_count=updated.architect_count,
                last_event=updated.last_event,
            )

    async def increment_architect_consult(self, task_id: str) -> "StuckState":
        """v0.26.1 patch G: bump ``architect_count`` for ``task_id``.

        Mirrors :meth:`increment_pivot` / :meth:`increment_search`. Used
        by the escalation ladder when the ARCHITECT_CONSULT rung fires.
        Threshold is 1 (one-shot per task) — after this call the next
        :func:`next_step` returns ``"SOFT_BLOCKER"``.
        """
        from orchestrator.escalation_ladder import StuckState

        async with plan_lock(self._cwd, timeout_s=self._lock_timeout_s):
            existing = self._stuck_state.get(task_id)
            if existing is None:
                existing = StuckState()
            updated = StuckState(
                discard_count=existing.discard_count,
                pivot_count=existing.pivot_count,
                search_count=existing.search_count,
                last_search_iter=existing.last_search_iter,
                architect_count=existing.architect_count + 1,
                last_event="architect_consult",
            )
            self._stuck_state[task_id] = updated
            return StuckState(
                discard_count=updated.discard_count,
                pivot_count=updated.pivot_count,
                search_count=updated.search_count,
                last_search_iter=updated.last_search_iter,
                architect_count=updated.architect_count,
                last_event=updated.last_event,
            )

    async def reset_stuck_state(self, task_id: str) -> None:
        """Zero the stuck-state counters for ``task_id`` (idempotent on unknown id).

        Called on successful task completion so future episodes start
        clean. Held under :func:`plan_lock`.
        """
        async with plan_lock(self._cwd, timeout_s=self._lock_timeout_s):
            self._stuck_state.pop(task_id, None)

    async def mark_task_retry(self, task_id: str) -> int:
        """Increment a task's ``retry_count``. Returns the new count.

        Does NOT change status — caller is responsible for transitioning
        back to ``in_progress`` (or escalating).

        v0.25.1 Bug #4: also stamps ``task.last_retry_at`` with the
        current UTC ISO timestamp and persists it in the ledger payload.
        ``_try_retry_or_escalate`` reads this to enforce
        ``qa_retry_min_interval_s`` across ``autodev resume`` boundaries.
        """
        async with plan_lock(self._cwd, timeout_s=self._lock_timeout_s):
            plan = self._load_sync()
            if plan is None:
                raise PlanConcurrentModificationError("no plan")
            task = _find_task(plan, task_id)
            if task is None:
                raise PlanConcurrentModificationError(f"unknown task {task_id}")
            task.retry_count += 1
            task.last_retry_at = _iso_now()
            new_count = task.retry_count
            await append_entry(
                self._cwd,
                op="update_task_status",
                payload={
                    "task_id": task_id,
                    "status": task.status,
                    "retry_count": new_count,
                    "last_retry_at": task.last_retry_at,
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
        max_corrective_tasks_per_phase: int | None = None,
        max_corrective_tasks_per_plan: int | None = None,
    ) -> Plan:
        """Append corrective sub-tasks to ``phase_id`` and update review_status.

        Mirrors :meth:`update_task_status`'s lock + ledger + snapshot
        pattern. The phase's ``corrective_task_ids`` and ``review_status``
        are updated atomically alongside the ``tasks`` list. Idempotent on
        replay (see :func:`state.ledger._apply_op` for the
        ``append_corrective_tasks`` op).

        v0.37.0 H2: when ``max_corrective_tasks_per_phase`` is supplied,
        the function enforces the cumulative per-phase cap defensively —
        if the phase's existing ``corrective_task_ids`` plus the new
        ``tasks`` would exceed the cap, the tail of ``tasks`` is
        truncated to fit and a ``corrective_cap_reached`` ledger op is
        appended with ``defended=True`` so dashboards can distinguish
        the defensive firing from the orchestrator-level upstream
        firing.

        v0.38.0 I3: when ``max_corrective_tasks_per_plan`` is supplied,
        the function ALSO enforces the cumulative plan-wide cap. The
        plan-scope check fires FIRST (it's the harder ceiling): the
        truncated batch is then subjected to the per-phase check on
        top, so both invariants hold on disk. The ``corrective_cap_reached``
        ledger op carries ``scope="plan"`` for the plan-scope firing and
        ``scope="phase"`` for the per-phase firing so dashboards can
        attribute the cap-hit to the right ceiling. Defence-in-depth:
        the orchestrator always computes the effective budget upstream
        and threads it through
        :func:`orchestrator.corrective_parser.parse_corrective_direction`,
        but a future caller bypassing that path would still hit these
        invariants.

        WS4: once the (possibly capped) batch is appended,
        :func:`orchestrator.dependency_inference.infer_dependencies` re-runs
        on the phase's FULL task list (existing + newly appended) before the
        ledger entry is written, so a newly-landed corrective task that
        shares a concrete file with an earlier same-phase task (the
        architect's original work or an earlier corrective round) picks up
        an inferred ``depends_on`` edge — closing the gap where corrective
        tasks, previously always ``files=[]``, were invisible to both
        overlap-avoidance mechanisms keyed on ``Task.files``. A no-op when
        nothing was actually appended (all-duplicate / fully-capped batch).

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

            # v0.38.0 I3: plan-scope defensive truncation. Fires BEFORE
            # the per-phase check because it's the harder ceiling — a
            # 24-task plan-wide budget with three 8-task phases ALL
            # at the per-phase ceiling collectively breaches the plan
            # cap before any individual phase does. The ``scope``
            # field on the emitted ``corrective_cap_reached`` op tells
            # dashboards which ceiling fired.
            tasks_to_append: list[Task] = list(tasks)
            if max_corrective_tasks_per_plan is not None:
                plan_cap = int(max_corrective_tasks_per_plan)
                total_plan_corrective = sum(
                    len(p.corrective_task_ids or []) for p in plan.phases
                )
                plan_remaining = max(0, plan_cap - total_plan_corrective)
                if len(tasks_to_append) > plan_remaining:
                    plan_dropped = len(tasks_to_append) - plan_remaining
                    tasks_to_append = tasks_to_append[:plan_remaining]
                    self._log.info(
                        "plan_manager.corrective_cap_defended",
                        scope="plan",
                        phase_id=phase_id,
                        cap=plan_cap,
                        dropped=plan_dropped,
                        total_plan_corrective=total_plan_corrective,
                    )
                    await append_entry(
                        self._cwd,
                        op="corrective_cap_reached",
                        payload={
                            "scope": "plan",
                            "phase_id": phase_id,
                            "cap": plan_cap,
                            "dropped": plan_dropped,
                            "defended": True,
                            "total_plan_corrective": total_plan_corrective,
                        },
                        session_id=self._session_id,
                    )

            # v0.37.0 H2: per-phase defensive cap check — truncate so the
            # on-disk invariant ``len(phase.corrective_task_ids) <= cap``
            # holds even if the caller skipped the upstream budget
            # computation. Operates on the (possibly already-trimmed)
            # ``tasks_to_append`` so the per-phase ``dropped`` count only
            # reflects the per-phase ceiling's contribution.
            defended_dropped = 0
            if max_corrective_tasks_per_phase is not None:
                cap = int(max_corrective_tasks_per_phase)
                existing_count = len(phase.corrective_task_ids or [])
                budget = max(0, cap - existing_count)
                if len(tasks_to_append) > budget:
                    defended_dropped = len(tasks_to_append) - budget
                    tasks_to_append = tasks_to_append[:budget]
                    self._log.info(
                        "plan_manager.corrective_cap_defended",
                        scope="phase",
                        phase_id=phase_id,
                        cap=cap,
                        dropped=defended_dropped,
                    )
                    await append_entry(
                        self._cwd,
                        op="corrective_cap_reached",
                        payload={
                            "scope": "phase",
                            "phase_id": phase_id,
                            "cap": cap,
                            "dropped": defended_dropped,
                            "defended": True,
                        },
                        session_id=self._session_id,
                    )

            existing_ids = {t.id for t in phase.tasks}
            appended: list[Task] = []
            for t in tasks_to_append:
                if t.id in existing_ids:
                    continue
                phase.tasks.append(t)
                existing_ids.add(t.id)
                if t.id not in phase.corrective_task_ids:
                    phase.corrective_task_ids.append(t.id)
                appended.append(t)

            if appended:
                # WS4: re-run implicit dependency inference on the phase's
                # FULL task list (existing + newly appended) right after the
                # append succeeds. Corrective tasks now carry real ``files``
                # (parsed from the "Scope strictly to:" clause by
                # :func:`orchestrator.corrective_parser.parse_corrective_direction`),
                # so a corrective that shares a file with an earlier
                # same-phase task (the architect's original work OR an
                # earlier corrective round) can be serialized after it
                # instead of racing it in a parallel worktree.
                #
                # Safe to re-run on an already-(partially-)inferred phase:
                # ``infer_dependencies`` only ever touches tasks whose
                # ``depends_on`` is CURRENTLY empty, so a task that already
                # carries an explicit or previously-inferred edge is never
                # revisited, and edges are assigned (not appended), so
                # calling this more than once can never duplicate an edge.
                # Every inferred edge points strictly backward in
                # declaration order, so re-running cannot introduce a cycle.
                # Lazy import: ``orchestrator`` imports ``state.plan_manager``
                # at package-init time (mirrors ``mark_blocked_descendants``'s
                # lazy ``orchestrator.dag`` import just below), so a top-level
                # import here would be a state→orchestrator import cycle.
                from orchestrator.dependency_inference import infer_dependencies

                infer_dependencies(phase)

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
        end_checkpoint_commit: str | None = None,
        metadata: dict | None = None,
    ) -> Plan:
        """Update phase-level metadata fields.

        Supported fields: ``baseline_commit`` (v0.9.0 / phase entry SHA),
        ``review_status`` (v0.9.0 / phase-review state machine), and
        ``end_checkpoint_commit`` (v0.21.0 B1 / phase completion SHA).
        Lock + ledger + snapshot. Any field can be ``None`` to leave
        unchanged. Mirrors :meth:`update_task_status` semantics so
        resumes / replays reproduce the metadata transitions exactly.

        v0.38.0 I3 (HK5): ``metadata`` (when supplied) is shallow-merged
        into :attr:`Phase.metadata` — existing keys are preserved, the
        delta's keys overwrite. The delta is recorded on the
        ``update_phase_meta`` payload so replay reproduces the merge.
        This is the substrate for HK5's ``skip_corrective_count``
        counter (tracked per-phase to detect stuck
        ``skip_corrective_round`` loops without minting a new typed
        field per knob).
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
            if end_checkpoint_commit is not None:
                phase.end_checkpoint_commit = end_checkpoint_commit
                payload["end_checkpoint_commit"] = end_checkpoint_commit
            if metadata is not None:
                merged = dict(phase.metadata or {})
                merged.update(metadata)
                phase.metadata = merged
                payload["metadata"] = dict(metadata)

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
                end_checkpoint_commit=end_checkpoint_commit,
                metadata_keys=list(metadata.keys()) if metadata else None,
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
        if "last_retry_at" in payload:
            # v0.25.1 Bug #4: restore last_retry_at on replay so
            # autodev resume can enforce qa_retry_min_interval_s.
            last_retry_at = payload["last_retry_at"]
            task.last_retry_at = (
                str(last_retry_at) if last_retry_at is not None else None
            )
        if "escalated" in payload:
            task.escalated = bool(payload["escalated"])
        if "evidence_bundle" in payload:
            task.evidence_bundle = payload["evidence_bundle"]
        # v0.32.0 (Phase 5, Gap G): replay the recovery_hint payload
        # back onto the task so resumed sessions surface the same hint
        # the original block site populated.
        if "recovery_hint" in payload:
            from state.schemas import RecoveryHint as _RecoveryHint

            raw_hint = payload["recovery_hint"]
            if raw_hint is None:
                task.recovery_hint = None
            else:
                try:
                    task.recovery_hint = _RecoveryHint.model_validate(raw_hint)
                except Exception:  # noqa: BLE001 - tolerate legacy payloads
                    task.recovery_hint = None
        # Step 5 (RECOVERY-CONTRACT §7 Part 3): replay the resolver guidance onto
        # ``Task.metadata`` so the snapshot fast-path's post-snapshot replay keeps
        # the same note the full-replay path (ledger._apply_op) restores.
        for _mkey in ("resolver_note", "resolver_action"):
            if _mkey not in payload:
                continue
            _new_md = dict(task.metadata or {})
            if payload[_mkey] is None:
                _new_md.pop(_mkey, None)
            else:
                _new_md[_mkey] = str(payload[_mkey])
            task.metadata = _new_md
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

    if op in (
        "stuck_refine",
        "stuck_pivot",
        "soft_blocker_handoff",
        "course_correction_emitted",
        # v0.26.1: architect-consult audit op (parity with
        # :func:`state.ledger._apply_op`).
        "architect_consult",
        # v0.26.2 Phase 3: persistent-failure drop audit op. Plan state
        # is captured by the ``init_plan`` entry written alongside.
        "scope_entry_dropped",
    ):
        # v0.15.0: ladder + PRM audit ops do not mutate plan state. Status
        # transitions are recorded by ``update_task_status`` separately.
        return plan

    if op == "hypothesis_repeat_detected":
        # v0.17.0 S4: advisory repeat-hypothesis tag (forensics only).
        return plan

    if op == "web_search_invoked":
        # v0.17.0 S2: audit-only forensics — does not mutate plan state.
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
        # v0.9.0: idempotent phase-meta update. v0.21.0 B1 adds
        # ``end_checkpoint_commit`` for cross-phase parallelism.
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
        if "end_checkpoint_commit" in payload:
            val = payload["end_checkpoint_commit"]
            phase.end_checkpoint_commit = val if isinstance(val, str) else None
        return plan

    if op in ("framing_classified", "framing_strategy_chosen"):
        # ADR-0044: audit-only breadcrumbs (see ledger._apply_op). No plan mutation.
        return plan

    if op in (
        "intake_assessed",
        "intake_gathered",
        "intake_enriched",
        "intake_questions_posed",
        "intake_answered",
        "intake_defaults_assumed",
        "spec_locked",
    ):
        # ADR-0045: audit-only breadcrumbs (see ledger._apply_op). No plan mutation.
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
