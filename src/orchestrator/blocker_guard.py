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
    except Exception:  # noqa: BLE001 - resolver must never break the block path
        recovered = None
    if recovered is not None and getattr(recovered, "status", None) != "blocked":
        return recovered
    return await orch.plan_manager.update_task_status(
        task.id, "blocked", meta=meta or {}
    )
