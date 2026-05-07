"""Unit tests for ``tournament.effort.resolve_role_effort``.

The resolver implements an explicit precedence chain:

    1. ``agent_cfg.effort`` (per-role override in ``.autodev/config.json``)
    2. ``role == "architect"``: ``ARCHITECT_EFFORT[user_complexity]`` (floor)
    3. ``role`` mapped in ``ROLE_TIER`` AND ``plan_complexity is not None``:
       ``EFFORT_MATRIX[plan_complexity][tier]``
    4. ``None`` (adapter omits ``--effort``; inherits Claude Code user-global)

These tests exercise each branch in isolation and verify the override
always wins, including for roles not in the matrix.
"""

from __future__ import annotations

import pytest

from config.schema import AgentConfig
from tournament.effort import (
    ARCHITECT_EFFORT,
    EFFORT_MATRIX,
    ROLE_TIER,
    resolve_role_effort,
)


# ---------------------------------------------------------------------------
# Architect floor (rule 2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "user_complexity,expected",
    [
        ("low", "xhigh"),
        ("medium", "xhigh"),
        ("high", "xhigh"),
        ("max", "max"),
    ],
)
def test_architect_floor_at_each_user_complexity(
    user_complexity: str, expected: str
) -> None:
    """Architect ignores plan_complexity (it runs before plan exists)."""
    assert (
        resolve_role_effort("architect", None, None, user_complexity) == expected
    )


@pytest.mark.parametrize("plan_complexity", ["simple", "medium", "complex"])
@pytest.mark.parametrize(
    "user_complexity,expected",
    [
        ("low", "xhigh"),
        ("medium", "xhigh"),
        ("high", "xhigh"),
        ("max", "max"),
    ],
)
def test_architect_ignores_plan_complexity(
    plan_complexity: str, user_complexity: str, expected: str
) -> None:
    """Even if plan_complexity is somehow set, architect still keys on user_complexity."""
    assert (
        resolve_role_effort("architect", None, plan_complexity, user_complexity)
        == expected
    )


# ---------------------------------------------------------------------------
# Author tier (rule 3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["architect_b", "synthesizer"])
@pytest.mark.parametrize("plan_complexity", ["simple", "medium", "complex"])
@pytest.mark.parametrize("user_complexity", ["low", "medium", "high", "max"])
def test_author_tier_matrix(
    role: str, plan_complexity: str, user_complexity: str
) -> None:
    """Author roles return the matrix value for their plan_complexity column."""
    expected = EFFORT_MATRIX[plan_complexity]["author"]
    assert (
        resolve_role_effort(role, None, plan_complexity, user_complexity)
        == expected
    )


# ---------------------------------------------------------------------------
# Evaluator tier (rule 3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["critic_t", "judge", "reviewer"])
@pytest.mark.parametrize(
    "plan_complexity,expected",
    [("simple", "low"), ("medium", "medium"), ("complex", "medium")],
)
def test_evaluator_tier_matrix(
    role: str, plan_complexity: str, expected: str
) -> None:
    """Evaluator roles read the evaluator column of EFFORT_MATRIX."""
    # user_complexity is irrelevant for non-architect resolution; pin one value.
    assert (
        resolve_role_effort(role, None, plan_complexity, "medium") == expected
    )


# ---------------------------------------------------------------------------
# Developer tier (rule 3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["developer", "test_engineer"])
@pytest.mark.parametrize(
    "plan_complexity,expected",
    [("simple", "medium"), ("medium", "high"), ("complex", "xhigh")],
)
def test_developer_tier_matrix(
    role: str, plan_complexity: str, expected: str
) -> None:
    """Developer-tier roles read the developer column of EFFORT_MATRIX."""
    assert (
        resolve_role_effort(role, None, plan_complexity, "medium") == expected
    )


# ---------------------------------------------------------------------------
# Explicit override (rule 1) always wins
# ---------------------------------------------------------------------------


def test_explicit_override_beats_matrix() -> None:
    """``agent_cfg.effort`` wins over the EFFORT_MATRIX value."""
    cfg = AgentConfig(effort="low")
    assert (
        resolve_role_effort("architect_b", cfg, "complex", "max") == "low"
    )


def test_explicit_override_beats_architect_floor() -> None:
    """``agent_cfg.effort`` wins over ``ARCHITECT_EFFORT``."""
    cfg = AgentConfig(effort="medium")
    assert resolve_role_effort("architect", cfg, None, "low") == "medium"


def test_explicit_override_beats_unknown_role_fallback() -> None:
    """Even for an unmapped role, an explicit override returns its value."""
    cfg = AgentConfig(effort="high")
    assert (
        resolve_role_effort("docs", cfg, "complex", "high") == "high"
    )


# ---------------------------------------------------------------------------
# Fallback to None (rule 4)
# ---------------------------------------------------------------------------


def test_unknown_role_returns_none() -> None:
    """Roles not in ROLE_TIER and != 'architect' fall through to None."""
    assert resolve_role_effort("docs", None, "complex", "high") is None


@pytest.mark.parametrize(
    "role", ["architect_b", "synthesizer", "critic_t", "judge", "reviewer", "developer", "test_engineer"]
)
@pytest.mark.parametrize("user_complexity", ["low", "medium", "high", "max"])
def test_plan_complexity_none_for_non_architect_returns_none(
    role: str, user_complexity: str
) -> None:
    """Without a parsed plan_complexity, only the architect resolves."""
    assert resolve_role_effort(role, None, None, user_complexity) is None


# ---------------------------------------------------------------------------
# AgentConfig with effort=None should NOT be treated as an override.
# ---------------------------------------------------------------------------


def test_agent_cfg_with_no_effort_falls_through() -> None:
    """``AgentConfig(effort=None)`` (the default) does not short-circuit."""
    cfg = AgentConfig()  # effort defaults to None
    # Should still hit the matrix.
    assert (
        resolve_role_effort("architect_b", cfg, "medium", "medium")
        == EFFORT_MATRIX["medium"]["author"]
    )


# ---------------------------------------------------------------------------
# Sanity: exported tables match the documented matrix
# ---------------------------------------------------------------------------


def test_architect_effort_table_is_complete() -> None:
    assert set(ARCHITECT_EFFORT) == {"low", "medium", "high", "max"}


def test_effort_matrix_is_complete() -> None:
    assert set(EFFORT_MATRIX) == {"simple", "medium", "complex"}
    for bucket in EFFORT_MATRIX.values():
        assert set(bucket) == {"author", "evaluator", "developer"}


def test_role_tier_covers_documented_roles() -> None:
    expected = {
        "architect_b",
        "synthesizer",
        "critic_t",
        "judge",
        "reviewer",
        "developer",
        "test_engineer",
    }
    assert set(ROLE_TIER) == expected
