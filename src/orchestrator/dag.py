"""DAG validation and scheduling helpers for execute_phase parallelism.

v0.11.0 introduces parallel task execution within a phase. Tasks declare
``depends_on`` (other task ids in the same phase) and ``files`` (paths
they will modify). The scheduler reads these two surfaces to decide
which tasks may run concurrently:

* :func:`validate_phase_dag` — runs once at phase entry. Detects
  references to undefined task ids and cycles before any worker spawns.

Errors are surfaced as :class:`DagValidationError` for the user-facing
"architect emitted a bad DAG" case. Programming errors inside this
module surface as plain :class:`ValueError` (per project convention).
"""

from __future__ import annotations

from errors import AutodevError
from state.schemas import Phase


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


__all__ = [
    "DagValidationError",
    "validate_phase_dag",
]
