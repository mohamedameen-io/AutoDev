"""Per-role Claude Code ``--effort`` resolution from plan + user complexity.

``claude -p`` accepts ``--effort {low,medium,high,xhigh,max}`` to control
test-time compute. AutoDev sets this per-role using a hard-coded matrix that
combines the user's declared task complexity (``AutodevConfig.user_complexity``)
with the architect's classification of the produced plan
(``Plan.complexity``). Per-role explicit overrides
(``AgentConfig.effort``) always win.

Resolution chain (highest priority first):

  1. ``agent_cfg.effort`` (explicit per-role override in ``.autodev/config.json``)
  2. ``role == "architect"``: ``ARCHITECT_EFFORT[user_complexity]``
  3. ``role`` mapped in ``ROLE_TIER`` AND ``plan_complexity is not None``:
     ``EFFORT_MATRIX[plan_complexity][tier]``
  4. ``None`` (the adapter omits ``--effort``; Claude Code inherits the
     user-global default in ``~/.claude/settings.json``)

This module has no dependency on the orchestrator state — callers pass in
the plan and config values directly. That makes it cheap to unit-test and
safe to import from the adapter or LLM client.
"""

from __future__ import annotations

from typing import Literal

from config.schema import AgentConfig

# Type aliases (purely documentational — Python's Literal cannot constrain
# string keys of a dict literal at runtime, so the tables below are
# ``dict[str, ...]``; mypy still treats the keys structurally).
UserComplexity = Literal["low", "medium", "high", "max"]
PlanComplexity = Literal["simple", "medium", "complex"]
Tier = Literal["author", "evaluator", "developer"]


# Architect-only effort floor. The architect always reasons heavily — at
# minimum ``xhigh`` for {low, medium, high} user complexity, escalating to
# ``max`` only when the user explicitly flagged the task as ``max``.
ARCHITECT_EFFORT: dict[str, str] = {
    "low": "xhigh",
    "medium": "xhigh",
    "high": "xhigh",
    "max": "max",
}


# Effort matrix: rows are parsed plan-complexity buckets; columns are
# downstream agent tiers. Authors reason most (they generate plans/code);
# evaluators reason less (judging is a structured ranking task); developers
# track the author column for cohesion. Tuned against the QNX audit which
# found ``xhigh`` wasted on judges/critic_t.
EFFORT_MATRIX: dict[str, dict[str, str]] = {
    "simple":  {"author": "medium", "evaluator": "low",    "developer": "medium"},
    "medium":  {"author": "high",   "evaluator": "medium", "developer": "high"},
    "complex": {"author": "xhigh",  "evaluator": "medium", "developer": "xhigh"},
}


# Role-to-tier mapping. Roles not listed here (e.g. ``architect``,
# ``explorer``, ``domain_expert``, ``docs``, ``designer``,
# ``critic_sounding_board``, ``critic_drift_verifier``) fall through to the
# ``None`` branch in :func:`resolve_role_effort` — they inherit Claude Code's
# user-global effort instead. The architect is special-cased above the
# matrix because its effort is governed by ``ARCHITECT_EFFORT`` (it runs
# before the plan exists).
ROLE_TIER: dict[str, str] = {
    "architect_b":   "author",
    "synthesizer":   "author",
    "critic_t":      "evaluator",
    "judge":         "evaluator",
    "reviewer":      "evaluator",
    "developer":     "developer",
    "test_engineer": "developer",
}


def resolve_role_effort(
    role: str,
    agent_cfg: AgentConfig | None,
    plan_complexity: str | None,
    user_complexity: str,
) -> str | None:
    """Return the ``--effort`` value for ``role``, or ``None`` to inherit.

    See module docstring for the full resolution chain.

    Args:
        role: Agent role name (e.g. ``"architect"``, ``"judge"``).
        agent_cfg: The role's ``AgentConfig`` if present in
            ``.autodev/config.json``, else ``None``. ``agent_cfg.effort`` is
            the highest-priority override.
        plan_complexity: One of ``"simple" | "medium" | "complex"`` parsed
            from the architect's ``COMPLEXITY:`` line, or ``None`` if no
            plan exists yet (architect phase) or the plan is from a
            pre-upgrade run.
        user_complexity: One of ``"low" | "medium" | "high" | "max"`` from
            ``AutodevConfig.user_complexity`` (CLI ``--complexity`` may
            override this before the resolver is called).

    Returns:
        The effort string to pass via ``--effort``, or ``None`` if the
        adapter should omit the flag entirely (inherit user-global).
    """
    # Rule 1: explicit per-role override wins.
    if agent_cfg is not None and agent_cfg.effort:
        return agent_cfg.effort

    # Rule 2: architect floor keyed by user_complexity (plan doesn't exist
    # yet at architect time, so plan_complexity is ignored here).
    if role == "architect":
        return ARCHITECT_EFFORT[user_complexity]

    # Rule 3: matrix lookup requires both a parsed plan_complexity and a
    # known role tier.
    if plan_complexity is None:
        return None
    tier = ROLE_TIER.get(role)
    if tier is None:
        return None
    return EFFORT_MATRIX[plan_complexity][tier]


__all__ = [
    "ARCHITECT_EFFORT",
    "EFFORT_MATRIX",
    "ROLE_TIER",
    "resolve_role_effort",
]
