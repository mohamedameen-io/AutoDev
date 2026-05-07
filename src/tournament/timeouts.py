"""Per-role subprocess timeouts keyed by tournament role + plan complexity.

Sibling to :mod:`tournament.effort`: that module governs Claude Code's
``--effort`` test-time-compute knob, this one governs the subprocess
``timeout_s`` budget. Both are looked up by ``(role, plan_complexity)`` and
threaded into the :class:`~tournament.llm.AdapterLLMClient` ctor by the
plan and impl tournament runners.

Motivation: in v0.5.3, complex-plan ``architect_b`` calls were seen timing
out at the global 600s default; raising the global default would inflate
cheap calls (judge, critic_t) that finish in seconds. This table targets
the escalation only at the long-reasoning roles on complex plans.

Defaults below match the v0.5.4 plan; explicit per-call overrides via
``cfg.tournaments.timeout_s`` can be added in a future release.
"""

from __future__ import annotations


# Role → complexity → seconds. Roles missing from the table fall through to
# the AdapterLLMClient default in :func:`resolve_role_timeout_s` (returns
# ``None``; caller keeps its existing default).
ROLE_TIMEOUT_S: dict[str, dict[str, int]] = {
    "architect_b":  {"simple": 600,  "medium": 600, "complex": 1200},
    "synthesizer":  {"simple": 300,  "medium": 600, "complex": 900},
    "critic_t":     {"simple": 300,  "medium": 300, "complex": 600},
    "judge":        {"simple": 300,  "medium": 300, "complex": 300},
}


def resolve_role_timeout_s(role: str, complexity: str | None) -> int | None:
    """Return the timeout in seconds for ``(role, complexity)``, or ``None``.

    ``None`` is returned when:
        - ``complexity`` is ``None`` (architect phase has no parsed Plan yet,
          or pre-upgrade run).
        - ``role`` is not in :data:`ROLE_TIMEOUT_S`.
        - ``complexity`` is outside ``{"simple", "medium", "complex"}``.

    Callers should treat ``None`` as "no per-role override" and use whatever
    default they hold themselves (the AdapterLLMClient default is 600s).
    """
    if complexity is None:
        return None
    by_complexity = ROLE_TIMEOUT_S.get(role)
    if by_complexity is None:
        return None
    return by_complexity.get(complexity)


__all__ = [
    "ROLE_TIMEOUT_S",
    "resolve_role_timeout_s",
]
