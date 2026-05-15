"""Per-(task_id, role) budget escalation on repeated ``error_max_turns``.

When the developer (or any role) returns ``subtype == "error_max_turns"``
multiple times in a row on the same ``(task_id, role)`` pair, the
orchestrator currently retries with the same ``max_turns`` and
``timeout_s`` budget — burning the retry quota without giving the agent
any more runway.

This module implements a simple escalation policy:

* 1st attempt: configured ``max_turns`` (today: 10 for developer,
  3 for reviewer; from :mod:`config.defaults`).
* 2nd attempt (if ``error_max_turns``): ``ceil(prior * 1.5)`` turns,
  ``+25%`` timeout.
* 3rd attempt (if ``error_max_turns``): ``ceil(prior * 2.0)`` turns,
  ``+50%`` timeout. Also emits a ``budget_escalation`` ledger /
  structured-log breadcrumb.
* 4th attempt (if STILL ``error_max_turns``): hard fail with
  diagnostic — ``budget escalation exhausted``.

Escalation ONLY fires for ``error_max_turns``. Other failure subtypes
(``timeout`` / ``parse_error`` / ``rate_limited`` / ``auth_failed`` /
etc.) do NOT trigger escalation; the existing retry / circuit-breaker
machinery already covers those.

A per-call ceiling caps both ``max_turns`` and ``timeout_s`` so a
misbehaving agent can't acquire unbounded budget.

The escalation state is keyed on ``(task_id, role)``; it resets when:

* the role on a given task succeeds (or fails with a different subtype),
* a new task is dispatched (different ``task_id``),
* the orchestrator instance is replaced (in-memory only — NOT persisted
  across resume, by design — repeated max-turns in a fresh session
  starts the escalation ladder over).

Pure functions live in :func:`escalate_budget`; the stateful tracker
lives in :class:`BudgetEscalationTracker` and is owned by the
``Orchestrator``.

Tests: ``tests/test_orchestrator_budget_escalation.py``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


# Hard ceiling on the per-call escalated ``max_turns``. A misbehaving
# agent that legitimately exhausts every escalation tier should not be
# able to acquire unbounded turns — this caps the ladder regardless of
# the configured base.
DEFAULT_MAX_TURNS_CEILING: int = 100

# Hard ceiling on the per-call escalated ``timeout_s`` (1 hour). Mirrors
# the ``max_turns`` ceiling rationale.
DEFAULT_TIMEOUT_S_CEILING: int = 3600

# Per-attempt escalation curves keyed by attempt index (0-based).
# Index 0 = first attempt — no escalation (use the configured base).
# Index 1 = second attempt — 1.5× turns, +25% timeout.
# Index 2 = third attempt — 2.0× turns, +50% timeout.
# Index 3+ = exhausted; caller should hard-fail.
_TURN_MULTIPLIERS: tuple[float, ...] = (1.0, 1.5, 2.0)
_TIMEOUT_MULTIPLIERS: tuple[float, ...] = (1.0, 1.25, 1.5)


# Maximum number of escalation rungs (excluding the base attempt). After
# this many consecutive ``error_max_turns`` retries the caller should
# treat the next attempt as a hard fail rather than escalating again.
MAX_ESCALATIONS: int = len(_TURN_MULTIPLIERS) - 1  # = 2


def escalate_budget(
    base_max_turns: int,
    base_timeout_s: int | None,
    attempt: int,
    *,
    max_turns_ceiling: int = DEFAULT_MAX_TURNS_CEILING,
    timeout_s_ceiling: int = DEFAULT_TIMEOUT_S_CEILING,
) -> tuple[int, int | None]:
    """Return the escalated ``(max_turns, timeout_s)`` for ``attempt``.

    ``attempt`` is the **0-based** index of the current attempt — so the
    very first call passes ``attempt=0`` and gets the base budget back
    unchanged. ``attempt=1`` is the second call (1.5× / +25%);
    ``attempt=2`` is the third (2.0× / +50%). Higher values are clamped
    to the third-attempt curve so callers that miscount don't accidentally
    grant unbounded budget — but callers are expected to hard-fail
    rather than reach this branch.

    ``base_timeout_s`` of ``None`` means "no explicit timeout — adapter
    applies its own default"; the function preserves that semantics by
    returning ``None`` even on escalation. The caller that wants a
    concrete escalated timeout should resolve the adapter default first,
    then pass it in.

    Both returned values are capped at the provided ceilings.
    """
    # Clamp attempt to the curve length; the policy says hard-fail past
    # the last entry, but defensive clamping prevents an IndexError if a
    # caller miscounts.
    safe_attempt = max(0, min(attempt, len(_TURN_MULTIPLIERS) - 1))

    turn_mult = _TURN_MULTIPLIERS[safe_attempt]
    timeout_mult = _TIMEOUT_MULTIPLIERS[safe_attempt]

    # ``ceil`` matches the plan spec (``ceil(prior * 1.5)``) so the bump
    # rounds up rather than down — a 3-turn base goes to 5, not 4.
    new_max_turns = int(math.ceil(base_max_turns * turn_mult))
    new_max_turns = max(1, min(new_max_turns, max_turns_ceiling))

    new_timeout_s: int | None
    if base_timeout_s is None:
        new_timeout_s = None
    else:
        new_timeout_s = int(math.ceil(base_timeout_s * timeout_mult))
        new_timeout_s = max(1, min(new_timeout_s, timeout_s_ceiling))

    return new_max_turns, new_timeout_s


@dataclass
class BudgetEscalationTracker:
    """In-memory per-(task_id, role) consecutive ``error_max_turns`` counter.

    The tracker is intentionally simple: a ``dict`` keyed on the
    composite ``(task_id, role)`` tuple. Owned by the ``Orchestrator``
    (one tracker per orchestrator instance). Not persisted — a fresh
    session resets all counters by design.

    Usage protocol (from the ``delegate()`` site):

    1. Before dispatching, call :meth:`current_attempt` to learn how
       many consecutive ``error_max_turns`` results have been observed
       for this ``(task_id, role)``. Use that count with
       :func:`escalate_budget` to compute the budget for this call.
    2. Call :meth:`is_exhausted` to check whether the next call would
       exceed the escalation ladder. If ``True``, hard-fail with the
       diagnostic from :attr:`exhaustion_diagnostic` instead of
       dispatching.
    3. After the adapter returns, call :meth:`record_result` with the
       result's ``subtype``. ``error_max_turns`` increments the counter;
       any other subtype (including success) clears it.

    Tests: ``tests/test_orchestrator_budget_escalation.py``.
    """

    max_escalations: int = MAX_ESCALATIONS
    _counters: dict[tuple[str, str], int] = field(default_factory=dict)

    # Diagnostic surfaced when the escalation ladder is exhausted. Kept
    # as a class constant so tests and the surrounding orchestrator code
    # can match on it without copy-pasting the string.
    exhaustion_diagnostic: str = (
        "budget escalation exhausted; consider raising defaults in "
        "`.autodev/config.json`"
    )

    def current_attempt(self, task_id: str, role: str) -> int:
        """Return the 0-based attempt index for the next dispatch.

        ``0`` means "no prior ``error_max_turns`` for this (task, role) —
        use the base budget unchanged." ``1`` means "one prior
        ``error_max_turns`` — apply the second-attempt curve." etc.
        """
        return self._counters.get((task_id, role), 0)

    def is_exhausted(self, task_id: str, role: str) -> bool:
        """Return True when the next dispatch would exceed the ladder.

        After ``max_escalations`` consecutive ``error_max_turns``, the
        ladder is exhausted and the caller should hard-fail rather than
        dispatch a fourth attempt.
        """
        return self._counters.get((task_id, role), 0) > self.max_escalations

    def record_result(self, task_id: str, role: str, subtype: str | None) -> None:
        """Update the counter based on the adapter result's ``subtype``.

        ``error_max_turns`` increments; anything else (including success
        or other failure subtypes) clears the counter for this
        ``(task_id, role)``. Other-failure subtypes deliberately reset
        the counter — the policy only escalates for *consecutive*
        ``error_max_turns``.
        """
        key = (task_id, role)
        if subtype == "error_max_turns":
            self._counters[key] = self._counters.get(key, 0) + 1
        else:
            # Reset on success OR on any non-max-turns failure subtype.
            self._counters.pop(key, None)

    def reset(self, task_id: str, role: str) -> None:
        """Explicitly clear the counter for ``(task_id, role)``.

        Mainly useful for tests; production code lets
        :meth:`record_result` handle the reset implicitly.
        """
        self._counters.pop((task_id, role), None)

    def reset_all(self) -> None:
        """Clear every counter. Useful for tests; not used in production."""
        self._counters.clear()


__all__ = [
    "BudgetEscalationTracker",
    "DEFAULT_MAX_TURNS_CEILING",
    "DEFAULT_TIMEOUT_S_CEILING",
    "MAX_ESCALATIONS",
    "escalate_budget",
]
