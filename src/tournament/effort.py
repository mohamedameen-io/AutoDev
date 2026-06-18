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


# B3: effort→intensity mapping for developer prompt modulation.
#
# The necessity ladder (B1) is always injected; this table *modulates* its
# intensity by effort level:
#   low    → "lite":        bias hard toward the minimal change; suppress
#             speculative optimization, refactoring, and scope expansion.
#   medium → standard:      no extra text — the baseline necessity ladder is
#             already the right instruction.
#   high   → "deeper-work": refactors/optimizations that genuinely improve
#             touched code are in scope (they are not "speculative" when the
#             task involves working inside those files anyway).
#   max    → same as high.
#
# NON-NEGOTIABLE (replicated in NECESSITY_LADDER_GUIDANCE): the safety /
# input-validation / error-handling / security carve-out holds at EVERY
# level. "minimal change" at low effort is NOT an excuse to drop security.
_INTENSITY_LITE = """\

## EFFORT INTENSITY: LITE (low effort)

You are operating in **lite** mode. Bias strongly toward the **minimal change**
that satisfies the task. Specifically:

- Make the smallest diff that correctly solves the stated requirement.
- Do NOT add speculative optimizations — improvements not required by the task.
- Do NOT perform speculative refactoring — clean-ups not motivated by the task.
- Do NOT expand scope beyond what the task explicitly requires.
- When in doubt between a minimal fix and a larger improvement, choose the fix.

SAFETY OVERRIDE (non-negotiable at every intensity level): safety, input
validation, error handling, and security work are NEVER suppressed by lite
mode. If the task requires fixing a security issue or adding validation,
proceed at full depth — "minimal change" is not an excuse to skip safety.
"""

_INTENSITY_DEEPER_WORK = """\

## EFFORT INTENSITY: DEEPER WORK (high/max effort)

You are operating in **deeper-work** mode. Refactors and optimizations that
genuinely improve the touched code are **in scope**:

- If you are already modifying a file and see a clear, low-risk improvement
  (a cleaner abstraction, a more efficient algorithm, better error handling),
  you MAY include it — it is not "speculative" when you are already there.
- Still apply the necessity ladder before adding new dependencies or modules.
- Do NOT expand scope into unrelated files; "deeper" means higher quality on
  what you already touch, not a wider blast radius.
"""

EFFORT_INTENSITY: dict[str, str] = {
    "low":    _INTENSITY_LITE,
    "medium": "",             # standard: necessity ladder alone is the baseline
    "high":   _INTENSITY_DEEPER_WORK,
    "max":    _INTENSITY_DEEPER_WORK,  # same as high
}


def effort_intensity_guidance(user_complexity: UserComplexity) -> str:
    """Return the effort-intensity guidance string for the given effort level.

    Maps ``user_complexity`` (``"low" | "medium" | "high" | "max"``) to an
    instruction fragment that modulates the B1 necessity ladder:

    - ``"low"``    → lite: minimal-change bias, suppress speculative work.
    - ``"medium"`` → standard: empty string (necessity ladder is the baseline).
    - ``"high"``   → deeper-work: refactors/optimizations in touched code OK.
    - ``"max"``    → same as ``"high"``.

    The safety/validation/security carve-out is **always** present regardless
    of intensity level; it is never suppressed by "lite" mode.
    """
    return EFFORT_INTENSITY.get(user_complexity, "")


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
    "EFFORT_INTENSITY",
    "EFFORT_MATRIX",
    "ROLE_TIER",
    "effort_intensity_guidance",
    "resolve_role_effort",
]
