"""``state.rewind`` — pure functions for ``autodev rewind --to-phase N``.

The ``rewind`` recovery surface (v0.29.0 Bug 9). When the corrective
auto-accept-after-guardrail-kill pathway flips a phase's
``review_status`` to ``"accepted"`` without a real
``phase_review_complete`` event in front of it (the bug that produced
"phase 1 accepted in 0.5s with empty diff"), the operator needs a way
to undo the false acceptance and roll the plan back to the last phase
that was actually reviewed.

Three pure functions split the concern:

  - :func:`detect_last_stable_phase` — replays the ledger and returns
    the id of the most recent phase whose acceptance was preceded by a
    matching ``phase_review_complete`` event with ``accept_phase=True``.
    Returns ``None`` when no phase was ever genuinely accepted.

  - :func:`compute_rewind_diff` — reads the current plan and returns
    the (tasks-to-reset, phases-to-reset, evidence-to-archive) triple
    so callers can preview the mutation before committing to it.

  - :func:`apply_rewind` — idempotent. Drives the
    :class:`PlanManager` mutations and moves evidence /
    tournament artifacts under
    ``.autodev/rewound/<timestamp>-<phase_id>/`` (NOT delete; preserved
    for forensics). Appends one ``op="rewind"`` audit ledger entry
    capturing the target phase + before/after counts.

The module deliberately has no global state — every function accepts
the working-directory root explicitly so the suite can drive it from
``CliRunner.isolated_filesystem`` without monkey-patching.
"""

from __future__ import annotations

import datetime as _dt
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from autologging import get_logger
from state.ledger import append_entry, read_entries, snapshot_plan
from state.paths import autodev_root, evidence_dir, tournaments_dir
from state.plan_manager import PlanManager
from state.schemas import Plan


logger = get_logger(component="rewind")


# Statuses considered "non-pending work" — anything in this set on a
# task within an after-target phase will be flipped back to ``pending``
# by :func:`apply_rewind`. Tasks already at ``pending`` are skipped so
# the apply call is idempotent (zero ledger entries on a second run).
_NON_PENDING_STATUSES: frozenset[str] = frozenset(
    {
        "in_progress",
        "coded",
        "auto_gated",
        "reviewed",
        "tested",
        "tournamented",
        "complete",
        "blocked",
        "skipped",
    }
)


@dataclass(frozen=True)
class RewindDiff:
    """Preview of what :func:`apply_rewind` would do.

    All three fields are deterministically ordered (plan-order for
    ids; alphabetical for paths) so the dry-run table is stable
    across invocations and reviewers can diff the output reliably.

    ``task_ids_to_reset`` are the tasks whose status would be reset
    to ``pending``. ``phase_ids_to_reset`` are the phases whose
    ``review_status`` would be cleared back to ``None``.
    ``evidence_paths_to_archive`` is the list of
    evidence/tournament artifacts that would be moved (NOT deleted)
    into ``.autodev/rewound/<ts>-<phase_id>/``.
    """

    target_phase_id: str
    task_ids_to_reset: list[str] = field(default_factory=list)
    phase_ids_to_reset: list[str] = field(default_factory=list)
    evidence_paths_to_archive: list[Path] = field(default_factory=list)


@dataclass(frozen=True)
class RewindResult:
    """Outcome of :func:`apply_rewind`.

    Mirrors :class:`state.plan_manager.RequeueResult` in shape so the
    CLI can report a uniform "X tasks, Y phases, Z artifacts" summary
    across the recovery surface.
    """

    target_phase_id: str
    reset_task_ids: list[str] = field(default_factory=list)
    reset_phase_ids: list[str] = field(default_factory=list)
    archive_dir: Path | None = None
    archived_paths: list[Path] = field(default_factory=list)


def detect_last_stable_phase(cwd: Path) -> str | None:
    """Replay the ledger and return the last genuinely-stable phase id.

    A phase is considered "genuinely stable" iff the ledger contains
    an ``update_phase_meta`` entry with
    ``payload.review_status == "accepted"`` AND the closest preceding
    ``phase_review_complete`` entry with the matching ``phase_id``
    carries ``accept_phase=True``. A force-accept that lacks a real
    review (the bug that motivated this whole module) is therefore
    skipped, and the detector falls back to the most recent
    genuinely-reviewed phase.

    Returns the phase id on success, or ``None`` when:
      - the ledger is empty / does not exist
      - no phase was ever genuinely accepted

    The returned id is the *most recent* qualifying phase, not the
    first — operators expect "rewind to last good state".
    """
    try:
        entries = read_entries(cwd)
    except FileNotFoundError:
        return None
    if not entries:
        return None

    # Track, per phase id, the accept_phase verdict of the latest
    # ``phase_review_complete`` event seen so far. We update this
    # in-flight as we walk the ledger forward, and consult it when an
    # ``update_phase_meta(review_status="accepted")`` event lands.
    last_review_verdict: dict[str, bool] = {}
    last_stable: str | None = None

    for entry in entries:
        if entry.op == "phase_review_complete":
            phase_id = entry.payload.get("phase_id")
            accept = entry.payload.get("accept_phase")
            if isinstance(phase_id, str) and isinstance(accept, bool):
                last_review_verdict[phase_id] = accept
            continue
        if entry.op == "update_phase_meta":
            phase_id = entry.payload.get("phase_id")
            if not isinstance(phase_id, str):
                continue
            if entry.payload.get("review_status") != "accepted":
                continue
            # Genuinely accepted iff the most recent matching
            # phase_review_complete carried accept_phase=True.
            if last_review_verdict.get(phase_id) is True:
                last_stable = phase_id

    return last_stable


def compute_rewind_diff(cwd: Path, target_phase_id: str) -> RewindDiff:
    """Return the ``(tasks, phases, evidence)`` triple for ``target_phase_id``.

    "After-target" semantics: every phase whose plan-order index is
    strictly greater than the target's index is in scope. Tasks in
    those phases that are not currently ``pending`` get listed for
    reset; phases whose ``review_status`` is non-``None`` get listed
    for clear; evidence + tournament artifacts referencing those
    phases get listed for archive.

    The function is read-only — :func:`apply_rewind` is the mutator.
    Returns an empty diff (no task / phase / artifact entries) when
    the plan is missing or the target phase id is not in the plan;
    callers should treat that as "nothing to do".
    """
    import asyncio

    pm = PlanManager(cwd, session_id="cli-rewind-diff")

    async def _load() -> Plan | None:
        return await pm.load()

    plan = asyncio.run(_load())
    if plan is None:
        return RewindDiff(target_phase_id=target_phase_id)

    phase_ids_in_order = [p.id for p in plan.phases]
    if target_phase_id not in phase_ids_in_order:
        return RewindDiff(target_phase_id=target_phase_id)

    target_idx = phase_ids_in_order.index(target_phase_id)
    after_target_phase_ids: list[str] = phase_ids_in_order[target_idx + 1 :]
    after_target_set = set(after_target_phase_ids)

    task_ids_to_reset: list[str] = []
    phase_ids_to_reset: list[str] = []
    for phase in plan.phases:
        if phase.id not in after_target_set:
            continue
        if phase.review_status is not None:
            phase_ids_to_reset.append(phase.id)
        for task in phase.tasks:
            if task.status in _NON_PENDING_STATUSES:
                task_ids_to_reset.append(task.id)

    evidence_paths_to_archive = _collect_artifacts_for_phases(
        cwd, after_target_phase_ids
    )

    return RewindDiff(
        target_phase_id=target_phase_id,
        task_ids_to_reset=task_ids_to_reset,
        phase_ids_to_reset=phase_ids_to_reset,
        evidence_paths_to_archive=evidence_paths_to_archive,
    )


async def apply_rewind(
    cwd: Path,
    target_phase_id: str,
    plan_manager: PlanManager,
) -> RewindResult:
    """Idempotently reset every phase strictly after ``target_phase_id``.

    For each affected task: flip status back to ``pending``, zero
    ``retry_count``, clear ``escalated`` + ``blocked_reason``. For
    each affected phase: clear ``review_status`` back to ``None``.
    Move (NOT delete) every evidence / tournament artifact referencing
    those phases into
    ``.autodev/rewound/<UTC-isoformat>-<target_phase_id>/`` — the
    archive lets a forensic post-mortem still inspect what was
    accepted by mistake. The archive directory is omitted from the
    result when nothing needed moving (idempotent re-run).

    Bypasses the FSM ``assert_transition`` check the way
    :meth:`PlanManager.requeue_tasks` does: ``blocked → pending`` and
    ``complete → pending`` are not legal automatic edges (would
    trigger spontaneous retries / lose work) but they ARE legal
    operator-driven edges, and the ledger captures the explicit
    ``op="rewind"`` audit breadcrumb so the transition is never
    silent.
    """
    plan = await plan_manager.load()
    if plan is None:
        return RewindResult(target_phase_id=target_phase_id)

    phase_ids_in_order = [p.id for p in plan.phases]
    if target_phase_id not in phase_ids_in_order:
        return RewindResult(target_phase_id=target_phase_id)

    target_idx = phase_ids_in_order.index(target_phase_id)
    after_target_phase_ids = phase_ids_in_order[target_idx + 1 :]
    after_target_set = set(after_target_phase_ids)

    # Pre-pass: enumerate the work so the audit breadcrumb captures
    # the full intent BEFORE any per-task / per-phase op lands. A
    # crash partway through the loop then leaves the breadcrumb
    # plus the subset of mutations that did land — replay
    # reconstructs the partial state, and a second
    # ``apply_rewind`` finishes the job (idempotent).
    tasks_to_reset: list[tuple[str, str]] = []  # (task_id, prior_status)
    phases_to_reset: list[str] = []
    for phase in plan.phases:
        if phase.id not in after_target_set:
            continue
        if phase.review_status is not None:
            phases_to_reset.append(phase.id)
        for task in phase.tasks:
            if task.status in _NON_PENDING_STATUSES:
                tasks_to_reset.append((task.id, task.status))

    artifact_paths = _collect_artifacts_for_phases(cwd, after_target_phase_ids)

    archive_dir: Path | None = None
    archived_paths: list[Path] = []
    if artifact_paths:
        archive_dir = _archive_artifacts(cwd, target_phase_id, artifact_paths)
        archived_paths = artifact_paths

    if not tasks_to_reset and not phases_to_reset and not archived_paths:
        # Pure no-op: do not even write the audit breadcrumb so a
        # repeated invocation post-completion writes zero ledger
        # entries (mirrors ``requeue_tasks`` semantics).
        return RewindResult(target_phase_id=target_phase_id)

    # Audit-only breadcrumb. Recorded BEFORE the per-task /
    # per-phase ops so a partial-write crash leaves the breadcrumb +
    # whichever subset of mutations landed; replay reconstructs the
    # partial state cleanly.
    archive_dir_str: str | None = None
    if archive_dir is not None:
        try:
            archive_dir_str = str(archive_dir.relative_to(cwd))
        except ValueError:
            archive_dir_str = str(archive_dir)
    await append_entry(
        cwd,
        op="rewind",
        payload={
            "target_phase_id": target_phase_id,
            "reset_task_ids": [tid for tid, _ in tasks_to_reset],
            "reset_phase_ids": list(phases_to_reset),
            "archive_dir": archive_dir_str,
            "archived_paths": [
                _rel_or_abs(cwd, p) for p in archived_paths
            ],
        },
        session_id=plan_manager.session_id,
    )

    # Per-task transitions. Bypass assert_transition by writing the
    # ledger entry directly — ``update_task_status`` would reject
    # ``complete → pending``. We use the same pattern as
    # :meth:`PlanManager.requeue_tasks`: explicit ledger entries +
    # final snapshot via the ``snapshot_plan`` helper.
    for task_id, _prior in tasks_to_reset:
        await append_entry(
            cwd,
            op="update_task_status",
            payload={
                "task_id": task_id,
                "status": "pending",
                "blocked_reason": None,
                "retry_count": 0,
                "escalated": False,
            },
            session_id=plan_manager.session_id,
        )

    # Per-phase review-status reset. ``PlanManager.update_phase_meta``
    # short-circuits on ``review_status=None`` (treats it as "leave
    # unchanged"), but the desired post-state IS ``None``. We emit
    # the explicit ``review_status: None`` payload via
    # ``ledger_append`` so replay's ``_apply_op`` code-path (which
    # special-cases ``isinstance(val, str)`` and otherwise stores
    # ``None``) lands the right value. Mirrors the pattern used by
    # :meth:`PlanManager.requeue_tasks`.
    for phase_id in phases_to_reset:
        await plan_manager.ledger_append(
            op="update_phase_meta",
            payload={"phase_id": phase_id, "review_status": None},
        )

    # Reload + snapshot so the on-disk plan reflects the new state
    # (the per-task / per-phase ops above only wrote ledger entries —
    # the in-memory plan is reconstructed via replay on the next
    # ``load``).
    refreshed = await plan_manager.load()
    if refreshed is not None:
        # Force a snapshot so subsequent ``load`` calls hit the fast
        # path instead of replaying the full ledger.
        await snapshot_plan(cwd, refreshed, session_id=plan_manager.session_id)

    logger.info(
        "rewind.applied",
        target_phase_id=target_phase_id,
        reset_task_count=len(tasks_to_reset),
        reset_phase_count=len(phases_to_reset),
        archive_dir=archive_dir_str,
        archived_paths=len(archived_paths),
    )
    return RewindResult(
        target_phase_id=target_phase_id,
        reset_task_ids=[tid for tid, _ in tasks_to_reset],
        reset_phase_ids=list(phases_to_reset),
        archive_dir=archive_dir,
        archived_paths=archived_paths,
    )


def _collect_artifacts_for_phases(cwd: Path, phase_ids: list[str]) -> list[Path]:
    """Return the evidence + tournament artifact paths referencing any
    of ``phase_ids``. Sorted alphabetically for deterministic preview.

    Naming conventions consulted (match what
    :mod:`orchestrator.execute_phase` and the phase-review runner
    write):

      - ``.autodev/evidence/<phase_id>.* `` — phase-level evidence.
      - ``.autodev/evidence/<phase_id>.<sub>-*.json`` — task-level
        evidence (task ids share the ``<phase>.<sub>`` prefix).
      - ``.autodev/tournaments/phase-review-<phase_id>*`` — phase
        review tournament artifacts.
      - ``.autodev/tournaments/<task_id>*`` — impl-tournament
        artifacts under tasks of the given phase.

    Returns an empty list when the corresponding directories do not
    exist (e.g. a brand-new workspace).
    """
    if not phase_ids:
        return []

    found: list[Path] = []
    ev = evidence_dir(cwd)
    if ev.exists():
        for path in ev.iterdir():
            if not path.is_file():
                continue
            for pid in phase_ids:
                # Task-level evidence: filename like "1.1-developer.json"
                # — the leading token before the first dot AFTER the
                # phase id is the sub-task. We match phase prefix +
                # boundary character.
                name = path.name
                if name == f"phase-{pid}-review.json" or name.startswith(
                    f"phase-{pid}-"
                ):
                    found.append(path)
                    break
                if name.startswith(f"{pid}.") or name == f"{pid}.patch":
                    found.append(path)
                    break

    td = tournaments_dir(cwd)
    if td.exists():
        for path in td.iterdir():
            for pid in phase_ids:
                if path.name.startswith(f"phase-review-{pid}"):
                    found.append(path)
                    break
                if path.name.startswith(f"council-{pid}.") or path.name.startswith(
                    f"impl-{pid}."
                ):
                    found.append(path)
                    break

    return sorted(set(found), key=lambda p: str(p))


def _archive_artifacts(
    cwd: Path, target_phase_id: str, paths: list[Path]
) -> Path:
    """Move ``paths`` into ``.autodev/rewound/<UTC-iso>-<phase_id>/``.

    Directory naming uses a ``YYYYMMDDTHHMMSSZ`` UTC stamp so the
    archive is deterministically sortable for forensics. Returns the
    archive directory (already created on disk). Existing files in
    the destination are overwritten — but a fresh timestamp prefix
    means realistic re-runs land in different directories, so this
    only matters in the synthetic-time pathological case.
    """
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_root = autodev_root(cwd) / "rewound" / f"{stamp}-{target_phase_id}"
    archive_root.mkdir(parents=True, exist_ok=True)

    for src in paths:
        if not src.exists():
            continue
        # Preserve the relative shape under .autodev/ so a forensic
        # walk can reconstruct origin: an evidence file lands under
        # ``rewound/.../evidence/`` and a tournament file under
        # ``rewound/.../tournaments/``.
        try:
            rel = src.relative_to(autodev_root(cwd))
        except ValueError:
            rel = Path(src.name)
        dst = archive_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst, ignore_errors=True)
            shutil.move(str(src), str(dst))
        else:
            if dst.exists():
                dst.unlink()
            shutil.move(str(src), str(dst))

    return archive_root


def _rel_or_abs(cwd: Path, path: Path) -> str:
    """Return ``path`` relative to ``cwd`` when possible, else absolute."""
    try:
        return str(path.relative_to(cwd))
    except ValueError:
        return str(path)


__all__ = [
    "RewindDiff",
    "RewindResult",
    "apply_rewind",
    "compute_rewind_diff",
    "detect_last_stable_phase",
]
