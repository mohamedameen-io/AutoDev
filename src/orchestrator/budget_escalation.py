"""Per-(scope_id, role) budget escalation on repeated ``error_max_turns``.

When the developer (or any role) returns ``subtype == "error_max_turns"``
multiple times in a row on the same ``(scope_id, role)`` pair, the
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

The escalation state is keyed on ``(scope_id, role)``; it resets when:

* the role on a given scope succeeds (or fails with a different subtype),
* a new scope is dispatched (different ``scope_id``),
* the orchestrator instance is replaced (in-memory only — NOT persisted
  across resume, by design — repeated max-turns in a fresh session
  starts the escalation ladder over).

v0.32.0 Phase 1.2: ``scope_id`` is the generic key dimension. The
execute-phase ``delegate()`` site uses the per-task ``task_id`` as the
scope (preserving v0.31.0 behaviour byte-for-byte). The plan-phase
architect-retry loop uses the literal ``"plan_phase"`` so its
recurring ``error_max_turns`` failures escalate independently from
any execute-phase task.

Pure functions live in :func:`escalate_budget`; the stateful tracker
lives in :class:`BudgetEscalationTracker` and is owned by the
``Orchestrator``.

Tests: ``tests/test_orchestrator_budget_escalation.py`` (per-task) and
``tests/test_plan_phase_budget_escalation.py`` (plan-phase scope).
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
    """In-memory per-(scope_id, role) consecutive ``error_max_turns`` counter.

    The tracker is intentionally simple: a ``dict`` keyed on the
    composite ``(scope_id, role)`` tuple. Owned by the ``Orchestrator``
    (one tracker per orchestrator instance). Not persisted — a fresh
    session resets all counters by design.

    ``scope_id`` is generic. The two production scopes today:

    * Execute-phase ``delegate()`` passes ``scope_id=task_id`` so each
      execute-phase task gets its own escalation ladder.
    * Plan-phase architect-retry loop passes ``scope_id="plan_phase"``
      so the architect's recurring ``error_max_turns`` failures
      escalate independently of any execute-phase task.

    Adding a new scope is just a matter of choosing a stable string;
    no schema or state migration required because the tracker is
    in-memory only.

    Usage protocol:

    1. Before dispatching, call :meth:`current_attempt` to learn how
       many consecutive ``error_max_turns`` results have been observed
       for this ``(scope_id, role)``. Use that count with
       :func:`escalate_budget` to compute the budget for this call.
       :meth:`escalate_for` is a convenience wrapper that does both.
    2. Call :meth:`is_exhausted` to check whether the next call would
       exceed the escalation ladder. If ``True``, hard-fail with the
       diagnostic from :attr:`exhaustion_diagnostic` instead of
       dispatching.
    3. After the adapter returns, call :meth:`record_result` with the
       result's ``subtype``. ``error_max_turns`` increments the counter;
       any other subtype (including success) clears it. Plan-phase
       call sites that work with raw subtypes can use
       :meth:`record_failure` instead.

    Back-compat: the existing ``record_result(task_id, role, subtype)``
    signature is preserved verbatim — execute-phase tests pass
    ``task_id`` as the positional ``scope_id``.

    Tests: ``tests/test_orchestrator_budget_escalation.py`` (per-task)
    and ``tests/test_plan_phase_budget_escalation.py`` (plan-phase).
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

    def current_attempt(self, scope_id: str, role: str) -> int:
        """Return the 0-based attempt index for the next dispatch.

        ``0`` means "no prior ``error_max_turns`` for this (scope, role) —
        use the base budget unchanged." ``1`` means "one prior
        ``error_max_turns`` — apply the second-attempt curve." etc.
        """
        return self._counters.get((scope_id, role), 0)

    def is_exhausted(self, scope_id: str, role: str) -> bool:
        """Return True when the next dispatch would exceed the ladder.

        After ``max_escalations`` consecutive ``error_max_turns``, the
        ladder is exhausted and the caller should hard-fail rather than
        dispatch a fourth attempt.
        """
        return self._counters.get((scope_id, role), 0) > self.max_escalations

    def escalate_for(
        self,
        scope_id: str,
        role: str,
        *,
        base_max_turns: int,
        base_timeout_s: int | None = None,
        max_turns_ceiling: int = DEFAULT_MAX_TURNS_CEILING,
        timeout_s_ceiling: int = DEFAULT_TIMEOUT_S_CEILING,
    ) -> tuple[int, int | None]:
        """Convenience: read the current attempt and return escalated budget.

        Mirrors the inline ``current_attempt`` + :func:`escalate_budget`
        sequence the execute-phase ``delegate()`` site does by hand. The
        plan-phase architect-retry loop uses this directly so its call
        site stays a single line. Same signature shape as
        :func:`escalate_budget` but reads the attempt index from this
        tracker rather than requiring the caller to pass it in.

        Returns ``(max_turns, timeout_s)`` for the next dispatch. When
        no prior ``error_max_turns`` has been recorded for this scope,
        the base budget is returned unchanged.
        """
        attempt = self.current_attempt(scope_id, role)
        return escalate_budget(
            base_max_turns,
            base_timeout_s,
            attempt,
            max_turns_ceiling=max_turns_ceiling,
            timeout_s_ceiling=timeout_s_ceiling,
        )

    def record_result(self, scope_id: str, role: str, subtype: str | None) -> None:
        """Update the counter based on the adapter result's ``subtype``.

        ``error_max_turns`` increments; anything else (including success
        or other failure subtypes) clears the counter for this
        ``(scope_id, role)``. Other-failure subtypes deliberately reset
        the counter — the policy only escalates for *consecutive*
        ``error_max_turns``.
        """
        key = (scope_id, role)
        if subtype == "error_max_turns":
            self._counters[key] = self._counters.get(key, 0) + 1
        else:
            # Reset on success OR on any non-max-turns failure subtype.
            self._counters.pop(key, None)

    def record_failure(
        self, scope_id: str, role: str, subtype: str | None
    ) -> None:
        """Plan-phase-friendly alias for :meth:`record_result`.

        The plan-phase architect-retry loop calls this on every
        ``error_max_turns`` / ``parse_failed`` result. Semantics are
        identical to :meth:`record_result`; the alias exists so plan-
        phase call sites can read as ``record_failure(...)`` rather
        than ``record_result(...)`` (which sounds like it should also
        accept successes).
        """
        self.record_result(scope_id, role, subtype)

    def reset(self, scope_id: str, role: str) -> None:
        """Explicitly clear the counter for ``(scope_id, role)``.

        Mainly useful for tests; production code lets
        :meth:`record_result` handle the reset implicitly.
        """
        self._counters.pop((scope_id, role), None)

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
