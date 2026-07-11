"""v0.42.1 F1 — the single, enforced chokepoint for terminal task blocks.

:func:`block_task` is the ONLY sanctioned way a task reaches ``blocked`` status.
It routes the failure through the Universal Blocker Resolver (ADR-0047) *first*
and commits ``update_task_status(..., "blocked")`` only if the resolver did not
recover the task. Likewise :func:`record_phase_degrade` (re-exported from
:mod:`orchestrator.blocker_resolver`) is the only sanctioned way a phase
degrades. The F1d enforcement test (``tests/test_block_path_invariant.py``)
fails if any *other* site commits a ``"blocked"`` transition directly, or if a
degrade-capable phase stops routing through ``record_phase_degrade`` — so a
silent dead-end (the Run-5 "resolver fired 0×" failure) is impossible to add by
construction, enforced in CI rather than by reviewer vigilance.

This module is deliberately import-light: it imports nothing heavy at module
load, so the low-level state layer (``state.plan_manager``, which cannot import
``orchestrator.execute_phase`` without a cycle) could call it. The resolver core
lives in :mod:`orchestrator.execute_phase` (``_maybe_resolve_blocker``); we reach
it via an optional ``orch.block_hook`` callback (registered at execute-phase
entry) or a *call-time* fallback import — never a module-level one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# Cycle-free re-export: blocker_resolver imports neither this module nor
# execute_phase, so this edge is one-directional. record_phase_degrade is the
# single, enforced degrade setter (F1b).
from orchestrator.blocker_resolver import record_phase_degrade

# WS3: a pure-constants module (imports only ``typing``) — no cycle risk, keeps
# this guard import-light. Supplies the conflict-exhaustion failure-class set
# the validated-patch recovery hook gates on.
from orchestrator import failure_classes as _fcls

# All five plan_manager "no plan initialized; call init_plan first" raises are
# ``PlanConcurrentModificationError`` (plan_manager.py:343/527/600/1330/1474),
# so the recovery control flow below matches on TYPE *and* signature. If a
# future plan_manager reword changes the message text, the isinstance gate makes
# the retry+breadcrumb fail SAFE (skipped) rather than silently mis-firing.
from errors import AskHumanDeadEndError, PlanConcurrentModificationError

if TYPE_CHECKING:  # pragma: no cover
    from orchestrator import Orchestrator
    from state.schemas import Task

__all__ = ["block_task", "record_phase_degrade"]


async def _resolve(
    orch: "Orchestrator",
    task: "Task | None",
    *,
    failure_class: str,
    raw_error: str,
    failing_role: str | None,
    phase_id: str | None,
    evidence_refs: list[str] | None,
) -> "Task | None":
    """Invoke the registered resolver chokepoint (``orch.block_hook``).

    Falls back to a *call-time* import of ``execute_phase._maybe_resolve_blocker``
    when no hook is registered (e.g. a unit test that calls ``block_task``
    directly). The call-time import is cycle-safe: by the time a block happens,
    ``execute_phase`` is fully loaded. Returns the recovered task or ``None``.
    """
    hook = getattr(orch, "block_hook", None)
    if hook is None:
        from orchestrator.execute_phase import _maybe_resolve_blocker

        hook = _maybe_resolve_blocker
    return await hook(
        orch,
        task,
        failure_class=failure_class,
        raw_error=raw_error,
        failing_role=failing_role,
        phase_id=phase_id,
        evidence_refs=evidence_refs,
    )


async def block_task(
    orch: "Orchestrator",
    task: "Task",
    *,
    failure_class: str,
    raw_error: str = "",
    failing_role: str | None = None,
    phase_id: str | None = None,
    evidence_refs: list[str] | None = None,
    meta: dict | None = None,
) -> "Task":
    """Resolve-or-block: the ONLY sanctioned committer of a ``blocked`` transition.

    Routes ``failure_class`` through the resolver. If the resolver actively
    recovers (re-enables) the task, returns that recovered task — its status is
    NOT ``blocked``. Otherwise commits ``update_task_status(..., "blocked", meta)``
    and returns the blocked task. NEVER raises out of the resolver path (the
    resolver is best-effort; a block must always be committable).
    """
    recovered: "Task | None" = None
    try:
        recovered = await _resolve(
            orch,
            task,
            failure_class=failure_class,
            raw_error=raw_error,
            failing_role=failing_role,
            phase_id=phase_id,
            evidence_refs=evidence_refs,
        )
    except AskHumanDeadEndError:
        # WS5 ``on_ask_human="fail"``: the ONE resolver outcome that must NOT be
        # swallowed into a silent block. Propagate the loud dead-end so a
        # fail-fast benchmark run exits non-zero rather than committing a
        # ``blocked`` transition here.
        raise
    except Exception:  # noqa: BLE001 - resolver must never break the block path
        recovered = None
    if recovered is not None and getattr(recovered, "status", None) != "blocked":
        return recovered

    # WS3: conflict-exhaustion validated-patch recovery. The resolver did NOT
    # recover this blocker. Before committing the terminal ``blocked``
    # transition, on EXACTLY the three conflict-exhaustion classes, attempt to
    # recover an already-VALIDATED patch (genuine reviewer APPROVED + converged
    # tournament winner) that was about to be discarded over a purely MECHANICAL
    # merge collision. The recovery COMPLETES the task (via the shared FSM-walk
    # ``_walk_task_to_complete``) and short-circuits this block — it NEVER
    # commits a ``blocked`` transition itself, so the F1a sole-committer
    # invariant (``tests/test_block_path_invariant.py``) is preserved: this
    # remains the only site that commits ``blocked``. Best-effort, mirroring the
    # resolver contract above — a recovery failure must NEVER break the block
    # path. The call-time import mirrors ``_resolve``'s fallback (cycle-safe: by
    # the time a block happens, ``execute_phase`` is fully loaded).
    if failure_class in _fcls.CONFLICT_EXHAUSTION_FAILURE_CLASSES:
        recovered_patch: "Task | None" = None
        try:
            from orchestrator.execute_phase import (
                _maybe_recover_validated_patch_on_conflict_exhaustion,
            )

            recovered_patch = (
                await _maybe_recover_validated_patch_on_conflict_exhaustion(
                    orch, task, failure_class=failure_class
                )
            )
        except Exception:  # noqa: BLE001 - recovery is best-effort; never break the block path
            recovered_patch = None
        if (
            recovered_patch is not None
            and getattr(recovered_patch, "status", None) != "blocked"
        ):
            return recovered_patch

    try:
        return await orch.plan_manager.update_task_status(
            task.id, "blocked", meta=meta or {}
        )
    except Exception as exc:  # noqa: BLE001
        # A1 (RECOVERY-CONTRACT §7 Part 4) belt-and-suspenders: the PRIMARY fix
        # for the field-observed ``"no plan initialized"`` on the
        # conflict→corrective path lives in
        # ``WorktreeManager.abort_failed_apply`` (it no longer lets a repo-wide
        # ``git clean -fd`` delete the untracked ``.autodev/`` ledger). This is
        # an IDEMPOTENT secondary guard ONLY: if the block commit still trips
        # "no plan initialized" — e.g. a TRANSIENT empty read while another
        # task's worktree git ops momentarily churn ``.autodev/`` — reload once
        # and retry the SAME terminal transition. The retry is safe to run
        # repeatedly (it either lands the single ``blocked`` transition against
        # the reloaded plan or re-raises with the breadcrumb below); it never
        # MASKS a genuine ledger loss (a physically-deleted ledger stays None on
        # reload and the original raise still propagates, attributed).
        if isinstance(exc, PlanConcurrentModificationError) and (
            "no plan initialized" in str(exc)
        ):
            try:
                # Probe whether the ledger is readable again; only retry if so.
                # (A genuinely deleted ledger stays None here and we fall through
                # to the breadcrumb + original raise — the transient retry never
                # MASKS a real loss.) The retry's fresh state comes from
                # ``update_task_status`` itself, which re-reads under the lock.
                reloaded = await orch.plan_manager.load()
            except Exception:  # noqa: BLE001 — probe best-effort
                reloaded = None
            if reloaded is not None:
                try:
                    return await orch.plan_manager.update_task_status(
                        task.id, "blocked", meta=meta or {}
                    )
                except Exception:  # noqa: BLE001 — fall through to breadcrumb
                    pass
        # Step 5 (RECOVERY-CONTRACT §7 Part 4) — defensive guard for the
        # field-observed ``worker_exception: "no plan initialized; call init_plan
        # first"`` on the conflict→corrective retry path (field-probes P5/P6). If
        # the terminal block commit fails because the PlanManager's ledger is
        # unexpectedly empty/absent, the un-guarded raise propagated up the worker
        # handler and was MISCLASSIFIED as a fresh ``worker_exception`` (masking
        # the real cause). We catch ONLY that specific "no plan initialized"
        # signature, emit an attributable breadcrumb so the missing-ledger
        # condition is never silent, and re-raise so a GENUINE state corruption is
        # still loud — but now correctly attributed (``block_path.plan_uninitialized``)
        # rather than surfacing as a spurious worker crash. The root mechanism was
        # not reproducible deterministically (see report); this guard makes any
        # recurrence diagnosable instead of self-masking.
        if isinstance(exc, PlanConcurrentModificationError) and (
            "no plan initialized" in str(exc)
        ):
            try:
                await orch.plan_manager.ledger_append(
                    op="block_path_plan_uninitialized",
                    payload={
                        "task_id": task.id,
                        "failure_class": failure_class,
                        "raw_error": (raw_error or "")[:300],
                        "err": str(exc)[:300],
                    },
                )
            except Exception:  # noqa: BLE001 - breadcrumb best-effort; never mask
                pass
        raise
