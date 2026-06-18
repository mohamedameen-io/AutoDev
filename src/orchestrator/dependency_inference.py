"""Post-parse implicit dependency inference for plan phases (v0.41.0 A2).

The scheduler (:meth:`state.plan_manager.PlanManager.next_pending_tasks`)
already serializes tasks on ``Task.depends_on`` — a task is not released
until every id in its ``depends_on`` reaches a terminal status. The parser
(:mod:`orchestrator.plan_parser`) already understands a ``- Depends:`` line.
The gap that produced the Run-3 parallel-worktree incoherence is upstream of
both: the **architect rarely emits dependencies**, so two tasks that share a
file (1.1 creates a serializer, 1.2 routes through it) run concurrently in
separate worktrees and the apply-to-main order is undefined.

This module closes that gap deterministically, *after* parsing and *before*
the plan is persisted/validated. It infers an implicit edge ``consumer
depends_on producer`` when, within the SAME phase:

* a later task's ``files`` / ``files_new`` overlap a concrete path an
  earlier task **creates or edits** (``files`` ∪ ``files_new``), OR
* a later task's *description* references an earlier task's id as a token
  (e.g. ``"route through the serializer created in 1.1"`` → depends on
  ``1.1``).

Conservatism guarantees (so inference never corrupts a hand-authored DAG):

* **Same-phase only.** Cross-phase ordering is handled by phase-major
  scheduling; we never infer across phase boundaries.
* **Earlier → later only.** "Earlier" is declaration order within the
  phase. Because every inferred edge points from a later task to a strictly
  earlier one, the added edges cannot introduce a cycle.
* **Only tasks missing an explicit ``depends_on``.** If the architect
  already declared dependencies for a task we leave it untouched — the
  human/agent signal wins. We never *append* to a non-empty list.
* **Concrete paths only.** Glob entries (``*``, ``?``, ``[``) are ignored
  for overlap purposes; resolving them needs the tracked-files cache and a
  fuzzy match is not conservative enough to silently serialize on.

The companion plan-gate warning lives in
:func:`orchestrator.dag.warn_unordered_file_sharers`, which flags the case
this module *cannot* repair: two same-phase tasks that share a file but where
neither depends on the other even after inference (e.g. both already carry
explicit but mutually-exclusive deps).
"""

from __future__ import annotations

import re

from autologging import get_logger
from state.schemas import Phase, Task

logger = get_logger(__name__)


# Mirrors :data:`orchestrator.dag._GLOB_CHARS` — kept local to avoid an
# import cycle and to keep this module dependency-light. A files entry
# containing any of these is a glob; we skip it for overlap inference
# because resolving it needs the tracked-files cache the parser does not
# have, and a fuzzy match is not conservative enough to silently serialize.
_GLOB_CHARS: frozenset[str] = frozenset("*?[")


def _is_glob(entry: str) -> bool:
    return any(c in _GLOB_CHARS for c in entry)


def _concrete_paths(values: list[str]) -> set[str]:
    """Return the literal (non-glob) entries of ``values`` as a set."""
    return {v for v in values if v and not _is_glob(v)}


def _produced_paths(task: Task) -> set[str]:
    """Paths ``task`` creates or edits — the surface a consumer can depend on.

    Both ``files`` (edited) and ``files_new`` (created) count: a later task
    that touches a file an earlier task edits must still be serialized after
    it, not just files the earlier task creates from scratch.
    """
    return _concrete_paths(task.files) | _concrete_paths(task.files_new)


def _consumed_paths(task: Task) -> set[str]:
    """Paths ``task`` reads/edits — the surface that creates a dependency."""
    return _concrete_paths(task.files) | _concrete_paths(task.files_new)


def _build_id_token_matchers(task_ids: list[str]) -> dict[str, re.Pattern[str]]:
    """Compile a word-boundary matcher per task id for description scanning.

    Task ids look like ``1.1`` / ``2.10`` / ``1a``. The matcher must hit a
    standalone token but never a substring of a longer id:

    * Left edge ``(?<![\\w.])`` rejects a preceding word char or ``.`` — so
      ``1.1`` does not match inside ``11.1``.
    * Right edge ``(?![\\w])(?!\\.\\d)`` rejects a following word char and a
      following ``.<digit>`` — so ``1.1`` does not match inside ``1.11`` or
      ``1.1.2``, but a trailing sentence period (``...created in 1.1.``) is
      still accepted because the ``.`` is not followed by a digit.

    The id is regex-escaped so its ``.`` is a literal, not a wildcard.
    """
    return {
        tid: re.compile(rf"(?<![\w.]){re.escape(tid)}(?![\w])(?!\.\d)")
        for tid in task_ids
    }


def infer_dependencies(phase: Phase) -> Phase:
    """Populate ``depends_on`` for same-phase tasks that imply a dependency.

    Mutates the tasks of ``phase`` in place (and returns ``phase`` for
    call-site convenience). Only tasks whose ``depends_on`` is currently
    **empty** are eligible — a task with any explicit dependency is left
    exactly as the architect declared it.

    For each eligible consumer task, in declaration order, an edge is added
    to every **strictly earlier** task that either:

    * produces (creates/edits) a concrete file the consumer also touches, or
    * is referenced by id as a standalone token in the consumer's
      description.

    Inferred dependency ids are de-duplicated and emitted in the earlier
    tasks' declaration order so the result is deterministic across runs.
    Self-references are impossible (a task is never "strictly earlier" than
    itself); because every edge points backward in declaration order, no
    cycle can be introduced.

    No-op for phases with zero or one task.
    """
    tasks = phase.tasks
    if len(tasks) < 2:
        return phase

    ids_in_order = [t.id for t in tasks]
    id_matchers = _build_id_token_matchers(ids_in_order)

    # Pre-compute the produced-path surface for every task once.
    produced: dict[str, set[str]] = {t.id: _produced_paths(t) for t in tasks}

    for idx, consumer in enumerate(tasks):
        # Conservatism: never touch a task the architect already ordered.
        if consumer.depends_on:
            continue

        earlier = tasks[:idx]
        if not earlier:
            continue

        consumer_files = _consumed_paths(consumer)
        desc = consumer.description or ""

        inferred: list[str] = []
        for producer in earlier:
            if producer.id == consumer.id:
                # Defensive: duplicate ids in a phase shouldn't happen, but
                # never make a task depend on a same-id sibling.
                continue
            reason: str | None = None
            # File overlap: consumer touches a file the producer creates/edits.
            if consumer_files and (consumer_files & produced[producer.id]):
                reason = "file_overlap"
            # Description id reference (e.g. "...created in 1.1").
            elif desc and id_matchers[producer.id].search(desc):
                reason = "description_reference"
            if reason is not None and producer.id not in inferred:
                inferred.append(producer.id)
                logger.info(
                    "dependency_inference.edge_added",
                    phase_id=phase.id,
                    consumer=consumer.id,
                    producer=producer.id,
                    reason=reason,
                )

        if inferred:
            # Emit in earlier-declaration order (already guaranteed by the
            # loop iterating ``earlier`` in order).
            consumer.depends_on = inferred

    return phase


def infer_plan_dependencies(phases: list[Phase]) -> list[Phase]:
    """Apply :func:`infer_dependencies` to every phase in ``phases``.

    Convenience wrapper for the plan-finalize path so callers don't loop.
    Mutates in place and returns the same list.
    """
    for phase in phases:
        infer_dependencies(phase)
    return phases


def repair_phase_edit_scope(
    phases: list[Phase], *, plan_edit_scope: list[str]
) -> list[Phase]:
    """Field-finding F-1: make each phase's ``edit_scope`` self-consistent
    with the files its own tasks declare.

    A plan is internally inconsistent when a phase declares a narrowing
    ``edit_scope`` (e.g. ``["index.js"]``) but tasks an edit to a file the
    scope does not list (e.g. a task whose ``files`` is ``["test_index.js"]``).
    The pre-flight per-task check
    (:func:`orchestrator.dag.collect_edit_scope_violations`) then raises
    ``edit_scope_violation`` and the task is blocked with an empty diff — it
    cannot deliver a file the plan itself asked it to edit.

    A task is, by definition, permitted to edit the files IT (or another task
    in the same phase) declares — those files ARE the plan's intent. This
    repair extends each phase's effective ``edit_scope`` to admit the
    concrete files its tasks declare (``files`` ∪ ``files_new``), so every
    downstream consumer (the pre-flight check, the sparse-checkout cone, the
    developer-prompt EDIT SCOPE addendum, and the recovery path) sees one
    self-consistent scope. Mutates phases in place and returns the same list.

    Resolution mirrors :func:`orchestrator.dag.validate_edit_scope`:

    * ``Phase.edit_scope`` non-None → that is the phase's resolved scope.
    * ``Phase.edit_scope is None`` → inherit ``plan_edit_scope``.
    * Resolved scope empty → whole-repo (no constraint). The repair is a
      **no-op** for such phases: materializing a scope here would silently
      narrow a whole-repo phase, the opposite of the intent.

    Safety boundary (preserved): only files SOME task in the phase declares
    are admitted. A path no task declares is never added, so the boundary
    against genuinely-undeclared files still holds — and the developer's
    ACTUAL edits are independently re-checked at apply time by
    :meth:`orchestrator.worktree.WorktreeManager.apply_patch_to_main`, which
    this repair does not touch.

    Glob entries in ``Task.files`` are skipped (consistent with
    dependency inference and with :func:`orchestrator.dag.is_in_scope`, which
    does prefix matching, not glob matching). A task that needs a glob beyond
    its scope still uses ``extended_scope``, which the validator unions in.
    """
    from orchestrator.dag import is_in_scope

    plan_scope = list(plan_edit_scope)
    for phase in phases:
        resolved = (
            list(phase.edit_scope) if phase.edit_scope is not None else plan_scope
        )
        # Empty resolved scope == whole-repo (legacy no-op). Never narrow it.
        if not resolved:
            continue

        declared: set[str] = set()
        for task in phase.tasks:
            # Union of files ∪ files_new: the full set of paths this task is
            # granted scope to edit (not created-only). _produced_paths and
            # _consumed_paths are identical here — a file the task edits is
            # also a file the task must be allowed to touch for scope purposes.
            declared |= _produced_paths(task)
        # Admit only the declared files not already covered by the scope,
        # in deterministic (sorted) order for stable diffs/ledgers.
        extra = sorted(p for p in declared if not is_in_scope(p, resolved))
        if not extra:
            continue

        # Trailing-slash normalization keeps these entries byte-identical to
        # what the schema validator would emit for an explicit EDIT_SCOPE
        # block (assignment does not re-run field validators on this model).
        new_scope = list(resolved) + [p.rstrip("/") for p in extra]
        if new_scope != phase.edit_scope:
            phase.edit_scope = new_scope
            logger.info(
                "dependency_inference.edit_scope_repaired",
                phase_id=phase.id,
                admitted=extra,
            )
    return phases


__all__ = [
    "infer_dependencies",
    "infer_plan_dependencies",
    "repair_phase_edit_scope",
]
