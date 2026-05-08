"""DAG validation and scheduling helpers for execute_phase parallelism.

v0.11.0 introduces parallel task execution within a phase. Tasks declare
``depends_on`` (other task ids in the same phase) and ``files`` (paths
they will modify). The scheduler reads these two surfaces to decide
which tasks may run concurrently:

* :func:`validate_phase_dag` — runs once at phase entry. Detects
  references to undefined task ids and cycles before any worker spawns.
* :func:`topological_levels` — Kahn's algorithm grouping by wave (level
  0 = no deps, level N = max(level of deps) + 1). Returned as a list of
  lists for consumers that want to spawn waves explicitly. The
  worker-pool dispatcher in :mod:`orchestrator.execute_phase` does NOT
  need this — it greedily polls :meth:`PlanManager.next_pending_tasks`.
  Levels remain useful for external consumers (CLI ``status --levels``,
  diagnostics, future scheduler heuristics) and for documenting what
  ``next_pending_tasks`` produces in aggregate.
* :func:`find_blocked_descendants` — given a set of failed task ids,
  returns every pending task whose ancestry includes any of them. The
  worker pool calls this after a task fails so dependents are
  cascade-blocked rather than left hanging.
* :func:`find_file_overlaps` — symmetric task-id → set-of-task-ids map
  derived from intersecting ``Task.files``. The dispatcher uses this to
  serialize concurrent execution of tasks touching the same files
  (worktree apply-time conflict avoidance).

Errors are surfaced as :class:`DagValidationError` for the user-facing
"architect emitted a bad DAG" case. Programming errors inside this
module surface as plain :class:`ValueError` (per project convention).
"""

from __future__ import annotations

from errors import AutodevError
from state.schemas import Phase, Task


class DagValidationError(AutodevError):
    """The phase's task DAG is structurally invalid.

    Carries a human-readable message that names the offending task ids
    and (for cycles) the full cycle path, so the user can fix the
    architect markdown directly without re-running the plan phase.
    """


def validate_phase_dag(phase: Phase) -> None:
    """Validate the DAG implied by ``Task.depends_on`` within ``phase``.

    Two failure modes:

    * **Undefined reference**: a task lists a ``depends_on`` id that is
      not present in the phase's task list. Raises
      :class:`DagValidationError` with both the offending task id and
      the missing dependency id.
    * **Cycle**: any cycle (including self-loops) is detected via DFS
      with a recursion stack. The full cycle path is included in the
      error message so the user can locate the loop quickly.

    No-op for empty phases.
    """
    task_ids = {t.id for t in phase.tasks}
    by_id = {t.id: t for t in phase.tasks}

    # Pass 1: undefined references (cheap; runs even if the cycle pass
    # would have caught the same issue indirectly via a missing node).
    for t in phase.tasks:
        for dep in t.depends_on:
            if dep not in task_ids:
                raise DagValidationError(
                    f"task {t.id!r} depends_on undefined task {dep!r} "
                    f"(phase {phase.id!r})"
                )

    # Pass 2: DFS cycle detection. ``WHITE`` = unvisited, ``GRAY`` =
    # in the current recursion stack, ``BLACK`` = fully processed.
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {tid: WHITE for tid in task_ids}

    def dfs(node: str, path: list[str]) -> None:
        color[node] = GRAY
        path.append(node)
        for dep in by_id[node].depends_on:
            if color[dep] == GRAY:
                # Cycle: slice path from the first occurrence of dep.
                start = path.index(dep)
                cycle = " -> ".join(path[start:] + [dep])
                raise DagValidationError(
                    f"cycle detected in phase {phase.id!r}: {cycle}"
                )
            if color[dep] == WHITE:
                dfs(dep, path)
        path.pop()
        color[node] = BLACK

    for tid in task_ids:
        if color[tid] == WHITE:
            dfs(tid, [])


def topological_levels(phase: Phase) -> list[list[Task]]:
    """Group ``phase.tasks`` into wave-levels via Kahn's algorithm.

    Level 0 = tasks with no ``depends_on`` (or whose deps are not in
    ``phase`` — though :func:`validate_phase_dag` should have caught
    that earlier). Level N = tasks all of whose ``depends_on`` resolve
    to tasks at levels < N.

    Returns ``[]`` for an empty phase. Returns ``[[task]]`` for a single
    task with no deps. Returns one task per level for a chain. For a
    diamond ``A -> {B, C} -> D``: ``[[A], [B, C], [D]]``.

    Tasks within a level are returned in the original ``phase.tasks``
    order (stable). Callers who want a deterministic execution order
    should rely on this stability.
    """
    if not phase.tasks:
        return []

    by_id = {t.id: t for t in phase.tasks}
    # Indegree = number of unresolved dependencies (filtered to deps in
    # this phase to be tolerant of cross-phase refs that the orchestrator
    # rejects elsewhere).
    indegree: dict[str, int] = {
        t.id: sum(1 for d in t.depends_on if d in by_id) for t in phase.tasks
    }
    # Reverse adjacency: dep_id → [task_ids that depend on dep_id].
    children: dict[str, list[str]] = {tid: [] for tid in by_id}
    for t in phase.tasks:
        for dep in t.depends_on:
            if dep in children:
                children[dep].append(t.id)

    levels: list[list[Task]] = []
    remaining = dict(indegree)
    # Stable iteration: pick zero-indegree tasks in original phase order.
    while remaining:
        wave_ids = [
            t.id for t in phase.tasks if t.id in remaining and remaining[t.id] == 0
        ]
        if not wave_ids:
            # Every task has an unresolved dep — implies a cycle. Should
            # never happen if validate_phase_dag ran first; raise the
            # same error type for symmetry.
            raise DagValidationError(
                f"cannot compute topological levels in phase {phase.id!r}: "
                "cycle or unresolved dependency"
            )
        levels.append([by_id[tid] for tid in wave_ids])
        for tid in wave_ids:
            del remaining[tid]
            for child in children[tid]:
                if child in remaining:
                    remaining[child] -= 1
    return levels


def find_blocked_descendants(
    phase: Phase, failed_task_ids: set[str]
) -> list[Task]:
    """Return every task in ``phase`` whose ancestry includes any failed id.

    Walks the reverse ``depends_on`` edges via BFS starting from each
    ``failed_task_ids``. Returns tasks in BFS-discovery order (callers
    should not rely on this order being stable beyond "deterministic
    given the same inputs").

    The ``failed_task_ids`` themselves are NOT included in the return —
    only their descendants. Tasks already in a terminal state (complete
    / blocked / skipped) ARE returned: the caller decides whether to
    re-block them. The plan_manager wrapper filters to "pending" tasks
    so already-terminal descendants are left alone.
    """
    if not failed_task_ids or not phase.tasks:
        return []

    by_id = {t.id: t for t in phase.tasks}
    # Reverse adjacency: dep_id → [task_ids that depend on dep_id].
    children: dict[str, list[str]] = {tid: [] for tid in by_id}
    for t in phase.tasks:
        for dep in t.depends_on:
            if dep in children:
                children[dep].append(t.id)

    visited: set[str] = set()
    descendants: list[Task] = []
    # Seed with the children of the failed tasks (NOT the failed tasks
    # themselves). Any failed_task_ids not in this phase are silently
    # ignored — caller's responsibility to scope correctly.
    queue: list[str] = []
    for fid in failed_task_ids:
        if fid in children:
            queue.extend(children[fid])

    while queue:
        nxt = queue.pop(0)
        if nxt in visited or nxt in failed_task_ids:
            continue
        visited.add(nxt)
        if nxt in by_id:
            descendants.append(by_id[nxt])
        queue.extend(children.get(nxt, []))

    return descendants


def find_file_overlaps(tasks: list[Task]) -> dict[str, set[str]]:
    """Map each task id to the set of OTHER task ids sharing >=1 ``files`` entry.

    The relation is symmetric: if A overlaps B, B overlaps A. Tasks with
    empty ``files`` lists never appear as keys with non-empty values
    (you can't conflict on no files).

    Returns a dict for every task id (including those with empty
    overlap sets) so callers can iterate without ``.get()``-with-default
    boilerplate.

    The dispatcher uses this map to refuse to start a task whose files
    intersect any in-flight task's files — apply-to-main conflict
    avoidance up-front rather than recover-after.
    """
    out: dict[str, set[str]] = {t.id: set() for t in tasks}
    for i, a in enumerate(tasks):
        a_files = set(a.files)
        if not a_files:
            continue
        for b in tasks[i + 1:]:
            if not b.files:
                continue
            if a_files & set(b.files):
                out[a.id].add(b.id)
                out[b.id].add(a.id)
    return out


__all__ = [
    "DagValidationError",
    "find_blocked_descendants",
    "find_file_overlaps",
    "topological_levels",
    "validate_phase_dag",
]
