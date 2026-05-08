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
    """``capacity.is_huge=True`` doubles the per-complexity bucket."""
    from runtime.repo_probe import RepoCapacity

    cap = RepoCapacity(
        file_count=25_000, total_bytes=10_000_000_000, depth_max=10, is_huge=True
    )
    assert resolve_task_max_turns(_task("simple"), spec_default=10, capacity=cap) == 20
    assert resolve_task_max_turns(_task("medium"), spec_default=10, capacity=cap) == 40
    assert resolve_task_max_turns(_task("complex"), spec_default=10, capacity=cap) == 80


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
