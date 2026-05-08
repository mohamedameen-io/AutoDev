"""Per-task ``max_turns`` and ``timeout_s`` overrides keyed by ``Task.complexity``.

Sibling to :mod:`tournament.timeouts` (per-role tournament timeouts) and
:mod:`tournament.effort` (per-role ``--effort``). This module governs the
per-task developer subprocess budget — the architect tags each task with
``- Complexity: simple|medium|complex`` and the resolvers below translate
the bucket into concrete ``max_turns`` / ``timeout_s`` values that
``orchestrator.execute_phase.delegate`` injects into the developer's
:class:`~adapters.types.AgentInvocation`.

Motivation: in v0.7.0 the developer's ``max_turns`` was capped uniformly at
``spec.max_turns`` (typically 10), which routinely escalated complex
investigation tasks to ``blocked`` after 3× ``error_max_turns`` retries.
Per-task scaling lets simple tasks stay cheap while genuinely complex ones
get the runway they need.

Resolver semantics:
    - ``task.complexity is None`` → return ``None`` (caller falls back to
      its own default — the spec value or a module-level constant).
    - ``task.complexity in TABLE`` → return the looked-up value.
    - Otherwise (defensive — schema corruption or unknown tokens) → return
      ``None``.

Pure functions; no orchestrator coupling. Tests live in
``tests/test_tournament_task_overrides.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from runtime.repo_probe import RepoCapacity
    from state.schemas import Task


# Per-task ``max_turns`` budget keyed by ``Task.complexity``. ``simple`` keeps
# the v0.7.0 default of 10; medium doubles to 20; complex bumps to 40 to give
# investigation/refactor tasks room to finish without burning retry budget on
# ``error_max_turns``.
TASK_MAX_TURNS_DEFAULTS: dict[str, int] = {
    "simple": 10,
    "medium": 20,
    "complex": 40,
}


# Per-task subprocess timeout (seconds) keyed by ``Task.complexity``. Mirrors
# the ``max_turns`` shape — simple stays at 10 minutes, medium at 20, complex
# at 30. Tuned so a ``complex`` task that legitimately uses 40 turns has wall
# time to finish before the watchdog fires.
TASK_TIMEOUT_S_DEFAULTS: dict[str, int] = {
    "simple": 600,
    "medium": 1200,
    "complex": 1800,
}


def resolve_task_max_turns(
    task: "Task",
    spec_default: int | None,
    capacity: "RepoCapacity | None" = None,
) -> int | None:
    """Return the per-task ``max_turns`` override or ``None``.

    ``spec_default`` is currently unused inside the resolver (the caller
    already knows it and falls back to it when this returns ``None``); it is
    accepted for symmetry with the future possibility of a multiplier-style
    resolution (e.g. ``2 * spec_default`` for medium). Including it now
    keeps the call sites stable.

    v0.13.0: optional ``capacity`` argument enables repo-size-aware scaling.
    When ``capacity.is_huge`` is True, the looked-up bucket value is
    multiplied per :data:`runtime.repo_probe._HUGE_MULTIPLIER` so genuinely
    complex tasks have runway on Unity-class repos. When ``capacity`` is
    None (legacy callers) or ``is_huge`` is False, behavior is unchanged.
    """
    if task.complexity is None:
        return None
    base = TASK_MAX_TURNS_DEFAULTS.get(task.complexity)
    if base is None:
        return None
    if capacity is not None and capacity.is_huge:
        # Local import: avoid a static cycle with runtime.repo_probe (which
        # imports ``TASK_MAX_TURNS_DEFAULTS`` from this module).
        from runtime.repo_probe import _HUGE_MULTIPLIER

        return int(round(base * _HUGE_MULTIPLIER))
    return base


def resolve_task_timeout_s(task: "Task", spec_default: int | None) -> int | None:
    """Return the per-task ``timeout_s`` override or ``None``.

    Same shape as :func:`resolve_task_max_turns` — see its docstring for the
    rationale on the ``spec_default`` parameter.
    """
    if task.complexity is None:
        return None
    return TASK_TIMEOUT_S_DEFAULTS.get(task.complexity)


__all__ = [
    "TASK_MAX_TURNS_DEFAULTS",
    "TASK_TIMEOUT_S_DEFAULTS",
    "resolve_task_max_turns",
    "resolve_task_timeout_s",
]
