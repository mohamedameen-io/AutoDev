"""v0.20.0 D1: per-bucket huge-repo multipliers."""

from __future__ import annotations

from runtime.repo_probe import RepoCapacity, resolve_max_turns


def _huge_cap() -> RepoCapacity:
    return RepoCapacity(
        file_count=25_000, total_bytes=10_000_000_000, depth_max=10, is_huge=True
    )


def _normal_cap() -> RepoCapacity:
    return RepoCapacity(
        file_count=1_000, total_bytes=10_000_000, depth_max=5, is_huge=False
    )


def test_default_per_bucket_multipliers_simple_3x() -> None:
    """Default curve: simple bucket on huge repo → 3.0× (10 → 30)."""
    assert resolve_max_turns("simple", _huge_cap(), base=None) == 30


def test_default_per_bucket_multipliers_medium_2x() -> None:
    """Default curve: medium bucket retains legacy 2.0× (20 → 40)."""
    assert resolve_max_turns("medium", _huge_cap(), base=None) == 40


def test_default_per_bucket_multipliers_complex_1_5x() -> None:
    """Default curve: complex bucket gets 1.5× (40 → 60)."""
    assert resolve_max_turns("complex", _huge_cap(), base=None) == 60


def test_normal_repo_no_multiplier_applied() -> None:
    """Non-huge repo: per-bucket curve is bypassed; raw lookup wins."""
    assert resolve_max_turns("simple", _normal_cap(), base=None) == 10
    assert resolve_max_turns("medium", _normal_cap(), base=None) == 20
    assert resolve_max_turns("complex", _normal_cap(), base=None) == 40


def test_operator_override_simple_4x() -> None:
    """Operator-supplied ``bucket_multipliers`` override the default."""
    overrides = {"simple": 4.0}
    assert (
        resolve_max_turns("simple", _huge_cap(), base=None, bucket_multipliers=overrides)
        == 40
    )


def test_operator_override_partial_falls_through_for_missing_buckets() -> None:
    """Buckets missing from override map use the baked-in default curve."""
    overrides = {"simple": 4.0}  # only override simple
    # medium / complex still use defaults
    assert (
        resolve_max_turns("medium", _huge_cap(), base=None, bucket_multipliers=overrides)
        == 40
    )
    assert (
        resolve_max_turns("complex", _huge_cap(), base=None, bucket_multipliers=overrides)
        == 60
    )


def test_explicit_base_uses_legacy_single_multiplier() -> None:
    """Operator-supplied ``base`` (no complexity) preserves legacy 2.0×."""
    # legacy path: base * _HUGE_MULTIPLIER (2.0) — backward compat
    assert resolve_max_turns("medium", _huge_cap(), base=15) == 30
    # bucket_multipliers should be ignored when base is set (no bucket key)
    assert (
        resolve_max_turns(
            "medium", _huge_cap(), base=15, bucket_multipliers={"medium": 5.0}
        )
        == 30
    )


def test_resolve_task_max_turns_threads_overrides() -> None:
    """``resolve_task_max_turns`` accepts and applies ``huge_repo_multipliers``."""
    from state.schemas import Task
    from tournament.task_overrides import resolve_task_max_turns

    cap = _huge_cap()
    task_simple = Task(
        id="1.1", phase_id="1", title="t", description="d", complexity="simple"
    )
    # default (no overrides) → 30 (3.0×)
    assert resolve_task_max_turns(task_simple, None, capacity=cap) == 30
    # override → 4.0× → 40
    assert (
        resolve_task_max_turns(
            task_simple, None, capacity=cap, huge_repo_multipliers={"simple": 4.0}
        )
        == 40
    )


def test_task_overrides_config_default_is_populated() -> None:
    """v0.36.0 E1: ``huge_repo_multipliers`` now defaults to a populated
    role-keyed dict (was ``None`` through v0.35). Operators no longer
    have to opt-in for huge-repo budget scaling on the standard roles.
    """
    from config.schema import TaskOverridesConfig

    cfg = TaskOverridesConfig()
    assert isinstance(cfg.huge_repo_multipliers, dict)
    assert cfg.huge_repo_multipliers.get("explorer") == 3.0


def test_task_overrides_config_accepts_override_dict() -> None:
    from config.schema import TaskOverridesConfig

    cfg = TaskOverridesConfig(
        huge_repo_multipliers={"simple": 5.0, "complex": 1.2}
    )
    assert cfg.huge_repo_multipliers is not None
    assert cfg.huge_repo_multipliers["simple"] == 5.0
    assert cfg.huge_repo_multipliers["complex"] == 1.2
