"""Tests for :mod:`tournament.task_overrides` — per-task ``max_turns`` /
``timeout_s`` overrides keyed by ``Task.complexity``.

Resolvers are pure functions — these tests assert table lookups and the
fallback behavior when ``task.complexity`` is ``None`` or schema-corrupt.
"""

from __future__ import annotations

from state.schemas import Task
from tournament.task_overrides import (
    TASK_MAX_TURNS_DEFAULTS,
    TASK_TIMEOUT_S_DEFAULTS,
    resolve_task_max_turns,
    resolve_task_timeout_s,
)


def _task(complexity: str | None) -> Task:
    """Build a minimal task fixture with the given complexity."""
    return Task(
        id="1.1",
        phase_id="1",
        title="t",
        description="d",
        complexity=complexity,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# resolve_task_max_turns
# ---------------------------------------------------------------------------


def test_resolve_max_turns_simple_returns_10() -> None:
    assert resolve_task_max_turns(_task("simple"), spec_default=10) == 10


def test_resolve_max_turns_medium_returns_20() -> None:
    assert resolve_task_max_turns(_task("medium"), spec_default=10) == 20


def test_resolve_max_turns_complex_returns_40() -> None:
    assert resolve_task_max_turns(_task("complex"), spec_default=10) == 40


def test_resolve_max_turns_none_complexity_returns_none() -> None:
    """When ``task.complexity is None``, the resolver returns ``None`` so the
    caller falls back to the spec default."""
    assert resolve_task_max_turns(_task(None), spec_default=10) is None


def test_resolve_max_turns_unknown_complexity_returns_none() -> None:
    """Defensive: a task that somehow holds an out-of-Literal value (schema
    corruption, manual plan.json edit) resolves to ``None`` rather than
    raising — the caller's spec default kicks in."""
    # Bypass pydantic to construct a corrupt task in-memory.
    task = _task("medium")
    object.__setattr__(task, "complexity", "trivial")
    assert resolve_task_max_turns(task, spec_default=10) is None


# ---------------------------------------------------------------------------
# resolve_task_timeout_s
# ---------------------------------------------------------------------------


def test_resolve_timeout_s_simple_returns_600() -> None:
    assert resolve_task_timeout_s(_task("simple"), spec_default=900) == 600


def test_resolve_timeout_s_medium_returns_1200() -> None:
    assert resolve_task_timeout_s(_task("medium"), spec_default=900) == 1200


def test_resolve_timeout_s_complex_returns_1800() -> None:
    assert resolve_task_timeout_s(_task("complex"), spec_default=900) == 1800


def test_resolve_timeout_s_none_complexity_returns_none() -> None:
    assert resolve_task_timeout_s(_task(None), spec_default=900) is None


def test_resolve_timeout_s_unknown_complexity_returns_none() -> None:
    task = _task("medium")
    object.__setattr__(task, "complexity", "fastest")
    assert resolve_task_timeout_s(task, spec_default=900) is None


# ---------------------------------------------------------------------------
# Table integrity — cheap guards against accidental edits
# ---------------------------------------------------------------------------


def test_max_turns_table_keys_match_complexity_literal() -> None:
    """The dict keys must mirror the three ``Task.complexity`` Literal values.
    A drift here would silently route a valid complexity to ``None``."""
    assert set(TASK_MAX_TURNS_DEFAULTS.keys()) == {"simple", "medium", "complex"}


def test_timeout_s_table_keys_match_complexity_literal() -> None:
    assert set(TASK_TIMEOUT_S_DEFAULTS.keys()) == {"simple", "medium", "complex"}


# ---------------------------------------------------------------------------
# v0.13.0: resolve_task_max_turns with optional capacity argument
#
# When ``capacity.is_huge`` is True, the resolver applies the same
# multiplier as ``runtime.repo_probe.resolve_max_turns``. When capacity is
# None or not huge, behavior is identical to the legacy resolver.
# ---------------------------------------------------------------------------


def test_resolve_task_max_turns_with_huge_capacity_applies_multiplier() -> None:
    """v0.20.0 D1: ``capacity.is_huge=True`` applies per-bucket curves
    (simple 3.0×, medium 2.0×, complex 1.5×)."""
    from runtime.repo_probe import RepoCapacity

    cap = RepoCapacity(
        file_count=25_000, total_bytes=10_000_000_000, depth_max=10, is_huge=True
    )
    assert resolve_task_max_turns(_task("simple"), spec_default=10, capacity=cap) == 30
    assert resolve_task_max_turns(_task("medium"), spec_default=10, capacity=cap) == 40
    assert resolve_task_max_turns(_task("complex"), spec_default=10, capacity=cap) == 60


def test_resolve_task_max_turns_capacity_none_preserves_legacy_behavior() -> None:
    """``capacity=None`` (default) reproduces the v0.12.0 lookup table."""
    assert resolve_task_max_turns(_task("simple"), spec_default=10, capacity=None) == 10
    assert resolve_task_max_turns(_task("medium"), spec_default=10, capacity=None) == 20
    assert resolve_task_max_turns(_task("complex"), spec_default=10, capacity=None) == 40


def test_resolve_task_max_turns_capacity_not_huge_preserves_lookup() -> None:
    """``capacity.is_huge=False`` is identical to the legacy lookup."""
    from runtime.repo_probe import RepoCapacity

    cap = RepoCapacity(
        file_count=1_000, total_bytes=10_000_000, depth_max=5, is_huge=False
    )
    assert resolve_task_max_turns(_task("simple"), spec_default=10, capacity=cap) == 10
    assert resolve_task_max_turns(_task("medium"), spec_default=10, capacity=cap) == 20


def test_resolve_task_max_turns_huge_with_none_complexity_returns_none() -> None:
    """``complexity=None`` still short-circuits to None even with huge cap.

    The caller's spec default kicks in — the multiplier never reaches it.
    """
    from runtime.repo_probe import RepoCapacity

    cap = RepoCapacity(
        file_count=25_000, total_bytes=10_000_000_000, depth_max=10, is_huge=True
    )
    assert resolve_task_max_turns(_task(None), spec_default=10, capacity=cap) is None


# ---------------------------------------------------------------------------
# v0.36.0 E1: huge_repo_multipliers populated default.
# ---------------------------------------------------------------------------


def test_resolve_max_turns_applies_huge_repo_multipliers() -> None:
    """When the dict carries the task complexity key AND the repo is
    huge, the multiplier wins over the baked-in curve."""
    from runtime.repo_probe import RepoCapacity

    cap = RepoCapacity(
        file_count=30_000, total_bytes=10_000_000_000, depth_max=12, is_huge=True
    )
    # Use complexity-keyed override (the resolver consults task.complexity).
    out = resolve_task_max_turns(
        _task("simple"),
        spec_default=10,
        capacity=cap,
        huge_repo_multipliers={"simple": 4.0},
    )
    assert out == 40  # 10 base * 4.0 override


def test_resolve_max_turns_unchanged_for_normal_repo() -> None:
    """A non-huge repo ignores huge_repo_multipliers entirely."""
    from runtime.repo_probe import RepoCapacity

    cap = RepoCapacity(
        file_count=500, total_bytes=1_000_000, depth_max=3, is_huge=False
    )
    out = resolve_task_max_turns(
        _task("medium"),
        spec_default=10,
        capacity=cap,
        huge_repo_multipliers={"medium": 99.0},
    )
    # Multiplier ignored — base medium = 20.
    assert out == 20


def test_huge_repo_multipliers_default_factory_is_populated() -> None:
    """v0.36.0 E1: config default is now a populated role-keyed dict
    (was ``None`` through v0.35)."""
    from config.schema import TaskOverridesConfig

    cfg = TaskOverridesConfig()
    assert isinstance(cfg.huge_repo_multipliers, dict)
    assert "explorer" in cfg.huge_repo_multipliers
    assert cfg.huge_repo_multipliers["explorer"] == 3.0


# ---------------------------------------------------------------------------
# v0.36.0 E2: retry budget scaling.
# ---------------------------------------------------------------------------


def test_retry_budget_doubles_on_attempt_2() -> None:
    """Retry attempt 2 doubles the resolved budget (default multiplier 2.0)."""
    out = resolve_task_max_turns(
        _task("medium"),
        spec_default=10,
        capacity=None,
        retry_attempt=2,
    )
    # medium base = 20 → 40 on retry attempt 2.
    assert out == 40


def test_retry_budget_capped_at_ceiling() -> None:
    """Retry budget can't exceed retry_budget_cap_turns."""
    out = resolve_task_max_turns(
        _task("complex"),
        spec_default=10,
        capacity=None,
        retry_attempt=3,
        retry_budget_multiplier=10.0,
        retry_budget_cap_turns=200,
    )
    # complex base = 40, 40 * 10.0 = 400, capped to 200.
    assert out == 200


def test_retry_attempt_0_or_1_unchanged() -> None:
    """No retry-scaling for attempts 0 and 1."""
    assert (
        resolve_task_max_turns(
            _task("medium"), spec_default=10, capacity=None, retry_attempt=0
        )
        == 20
    )
    assert (
        resolve_task_max_turns(
            _task("medium"), spec_default=10, capacity=None, retry_attempt=1
        )
        == 20
    )
