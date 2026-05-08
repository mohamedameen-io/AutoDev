"""Tests for src.config schema and loader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from config.defaults import default_config
from config.loader import expand_paths, load_config, save_config
from config.schema import AgentConfig, REQUIRED_AGENT_ROLES, AutodevConfig
from errors import ConfigError


def test_default_config_validates() -> None:
    cfg = default_config()
    dumped = cfg.model_dump(mode="json")
    reloaded = AutodevConfig.model_validate(dumped)
    # All required roles present.
    for role in REQUIRED_AGENT_ROLES:
        assert role in reloaded.agents
    assert reloaded.schema_version == "1.0.0"
    assert reloaded.tournaments.impl.num_judges == 1
    assert reloaded.tournaments.impl.convergence_k == 1
    assert reloaded.tournaments.impl.max_rounds == 3
    assert reloaded.tournaments.plan.enabled is True


def test_config_roundtrip(tmp_path: Path) -> None:
    cfg = default_config()
    path = tmp_path / ".autodev" / "config.json"
    save_config(cfg, path)

    loaded = load_config(path)
    assert loaded.model_dump(mode="json") == cfg.model_dump(mode="json")


def test_invalid_config_raises(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_missing_config_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.json")


def test_missing_agents_rejected(tmp_path: Path) -> None:
    cfg = default_config()
    data = cfg.model_dump(mode="json")
    # Remove several required roles.
    for role in ("developer", "judge", "architect"):
        data["agents"].pop(role, None)

    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "missing required agent roles" in str(exc.value)


def test_schema_version_stub() -> None:
    # Placeholder for future migrations: schema_version is a Literal["1.0.0"]
    # so any change must bump this test.
    cfg = default_config()
    assert cfg.schema_version == "1.0.0"


def test_expand_paths_resolves_home() -> None:
    cfg = default_config()
    assert str(cfg.hive.path).startswith("~")
    expanded = expand_paths(cfg)
    assert not str(expanded.hive.path).startswith("~")
    # Original config should be untouched.
    assert str(cfg.hive.path).startswith("~")


def test_agent_config_accepts_max_turns() -> None:
    cfg = AgentConfig(model="sonnet", max_turns=5)
    assert cfg.max_turns == 5


def test_agent_config_max_turns_default_none() -> None:
    cfg = AgentConfig()
    assert cfg.max_turns is None


def test_unknown_top_level_field_rejected(tmp_path: Path) -> None:
    cfg = default_config()
    data = cfg.model_dump(mode="json")
    data["unexpected_field"] = "oops"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


# ---------------------------------------------------------------------------
# AgentConfig.effort — per-role test-time-compute override
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "level",
    ["low", "medium", "high", "xhigh", "max", None],
)
def test_agent_config_accepts_effort_levels(level: str | None) -> None:
    cfg = AgentConfig(effort=level)
    assert cfg.effort == level
    # Round-trip through JSON to confirm the field persists.
    dumped = cfg.model_dump(mode="json")
    reloaded = AgentConfig.model_validate(dumped)
    assert reloaded.effort == level


def test_agent_config_rejects_invalid_effort() -> None:
    with pytest.raises(ValidationError):
        AgentConfig(effort="insane")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AutodevConfig.user_complexity — top-level user-declared complexity bucket
# ---------------------------------------------------------------------------


def test_autodev_config_user_complexity_default_medium() -> None:
    """A minimal valid config that doesn't set user_complexity defaults to
    ``"medium"`` — ensures pre-existing on-disk configs validate without
    migration.
    """
    cfg = default_config()
    data = cfg.model_dump(mode="json")
    # Drop the user_complexity field so we test the actual default.
    data.pop("user_complexity", None)
    reloaded = AutodevConfig.model_validate(data)
    assert reloaded.user_complexity == "medium"


@pytest.mark.parametrize("level", ["low", "medium", "high", "max"])
def test_autodev_config_user_complexity_accepts_levels(level: str) -> None:
    cfg = default_config()
    data = cfg.model_dump(mode="json")
    data["user_complexity"] = level
    reloaded = AutodevConfig.model_validate(data)
    assert reloaded.user_complexity == level
    # Round-trip again to confirm it persists.
    redumped = reloaded.model_dump(mode="json")
    rereloaded = AutodevConfig.model_validate(redumped)
    assert rereloaded.user_complexity == level


def test_autodev_config_rejects_invalid_user_complexity() -> None:
    """``"complex"`` is valid for ``Plan.complexity`` but NOT for
    ``AutodevConfig.user_complexity`` — they are distinct enums.
    """
    cfg = default_config()
    data = cfg.model_dump(mode="json")
    data["user_complexity"] = "complex"
    with pytest.raises(ValidationError):
        AutodevConfig.model_validate(data)


# ---------------------------------------------------------------------------
# v0.7.0 — TournamentPhaseConfig.complex_plan_num_judges_override (Issue 5C)
# ---------------------------------------------------------------------------


def test_tournament_phase_config_complex_plan_num_judges_override_round_trip() -> None:
    """The v0.7.0 ``complex_plan_num_judges_override`` field round-trips
    through ``model_dump`` + ``model_validate`` cleanly. Default is ``None``.
    """
    from config.schema import TournamentPhaseConfig

    base = TournamentPhaseConfig(
        enabled=True,
        num_judges=5,
        convergence_k=2,
        max_rounds=15,
    )
    assert base.complex_plan_num_judges_override is None

    with_override = TournamentPhaseConfig(
        enabled=True,
        num_judges=5,
        convergence_k=2,
        max_rounds=15,
        complex_plan_num_judges_override=7,
    )
    assert with_override.complex_plan_num_judges_override == 7
    reloaded = TournamentPhaseConfig.model_validate(with_override.model_dump())
    assert reloaded.complex_plan_num_judges_override == 7


def test_tournament_phase_config_complex_plan_num_judges_override_accepts_none() -> None:
    """Explicit ``None`` round-trips and signals 'feature off'."""
    from config.schema import TournamentPhaseConfig

    cfg = TournamentPhaseConfig(
        enabled=True,
        num_judges=5,
        convergence_k=2,
        max_rounds=15,
        complex_plan_num_judges_override=None,
    )
    assert cfg.complex_plan_num_judges_override is None
    reloaded = TournamentPhaseConfig.model_validate(cfg.model_dump())
    assert reloaded.complex_plan_num_judges_override is None


# ---------------------------------------------------------------------------
# v0.9.0 — TournamentsConfig.phase_review default factory
# ---------------------------------------------------------------------------


def test_phase_review_field_default_factory_returns_enabled_true() -> None:
    """The factory returns a fully-formed phase_review block — default-on."""
    cfg = default_config()
    assert cfg.tournaments.phase_review.enabled is True
    assert cfg.tournaments.phase_review.num_judges == 3
    assert cfg.tournaments.phase_review.convergence_k == 1
    assert cfg.tournaments.phase_review.max_rounds == 2


def test_legacy_config_without_phase_review_loads_via_default(tmp_path: Path) -> None:
    """A v0.8.0-shape config (no ``phase_review`` block under tournaments)
    validates via the field's default factory without raising.

    This is the migration guarantee: existing on-disk configs from v0.8.0
    keep loading after v0.9.0 ships.
    """
    cfg = default_config()
    data = cfg.model_dump(mode="json")
    # Strip the phase_review block from the serialized config to mimic
    # a v0.8.0 file.
    data["tournaments"].pop("phase_review", None)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    reloaded = load_config(path)
    assert reloaded.tournaments.phase_review.enabled is True
    assert reloaded.tournaments.phase_review.num_judges == 3


# ---------------------------------------------------------------------------
# v0.10.0 — TournamentsConfig.max_parallel_subprocesses: int | None
# ---------------------------------------------------------------------------


def test_max_parallel_subprocesses_accepts_none() -> None:
    """v0.10.0: ``max_parallel_subprocesses`` accepts ``None`` (auto-resolve
    via :func:`runtime.resource_probe.resolve_parallelism`)."""
    from config.schema import TournamentPhaseConfig, TournamentsConfig

    base_phase = TournamentPhaseConfig(
        enabled=True, num_judges=5, convergence_k=2, max_rounds=15
    )
    cfg = TournamentsConfig(
        plan=base_phase,
        impl=base_phase,
        max_parallel_subprocesses=None,
    )
    assert cfg.max_parallel_subprocesses is None
    # Round-trip: None survives serialize + reload.
    reloaded = TournamentsConfig.model_validate(cfg.model_dump())
    assert reloaded.max_parallel_subprocesses is None


def test_max_parallel_subprocesses_accepts_int() -> None:
    """An explicit int passes validation unchanged (backward-compat)."""
    from config.schema import TournamentPhaseConfig, TournamentsConfig

    base_phase = TournamentPhaseConfig(
        enabled=True, num_judges=5, convergence_k=2, max_rounds=15
    )
    cfg = TournamentsConfig(
        plan=base_phase,
        impl=base_phase,
        max_parallel_subprocesses=8,
    )
    assert cfg.max_parallel_subprocesses == 8
    reloaded = TournamentsConfig.model_validate(cfg.model_dump())
    assert reloaded.max_parallel_subprocesses == 8


# ---------------------------------------------------------------------------
# v0.11.0 — TournamentsConfig.execute_max_parallel_tasks: int | None
# ---------------------------------------------------------------------------


def test_execute_max_parallel_tasks_accepts_none() -> None:
    """v0.11.0: ``execute_max_parallel_tasks`` defaults to None (auto-resolve)."""
    from config.schema import TournamentPhaseConfig, TournamentsConfig

    base_phase = TournamentPhaseConfig(
        enabled=True, num_judges=5, convergence_k=2, max_rounds=15
    )
    cfg = TournamentsConfig(plan=base_phase, impl=base_phase)
    assert cfg.execute_max_parallel_tasks is None
    reloaded = TournamentsConfig.model_validate(cfg.model_dump())
    assert reloaded.execute_max_parallel_tasks is None


def test_execute_max_parallel_tasks_accepts_int() -> None:
    """An explicit int passes validation unchanged."""
    from config.schema import TournamentPhaseConfig, TournamentsConfig

    base_phase = TournamentPhaseConfig(
        enabled=True, num_judges=5, convergence_k=2, max_rounds=15
    )
    cfg = TournamentsConfig(
        plan=base_phase,
        impl=base_phase,
        execute_max_parallel_tasks=4,
    )
    assert cfg.execute_max_parallel_tasks == 4
    reloaded = TournamentsConfig.model_validate(cfg.model_dump())
    assert reloaded.execute_max_parallel_tasks == 4


def test_legacy_config_without_execute_max_parallel_tasks_loads() -> None:
    """A config dict missing ``execute_max_parallel_tasks`` validates and
    defaults the field to ``None`` (backward-compat)."""
    from config.schema import TournamentPhaseConfig, TournamentsConfig

    base_phase = TournamentPhaseConfig(
        enabled=True, num_judges=5, convergence_k=2, max_rounds=15
    )
    legacy_payload = TournamentsConfig(plan=base_phase, impl=base_phase).model_dump()
    legacy_payload.pop("execute_max_parallel_tasks", None)
    reloaded = TournamentsConfig.model_validate(legacy_payload)
    assert reloaded.execute_max_parallel_tasks is None


def test_legacy_config_with_max_parallel_int_still_loads(tmp_path: Path) -> None:
    """A pre-v0.10.0 config with ``max_parallel_subprocesses: 3`` keeps loading
    cleanly after the type widens to ``int | None``."""
    cfg = default_config()
    data = cfg.model_dump(mode="json")
    # Force a legacy explicit int (overrides v0.10.0's default of None).
    data["tournaments"]["max_parallel_subprocesses"] = 3
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    reloaded = load_config(path)
    assert reloaded.tournaments.max_parallel_subprocesses == 3


# ---------------------------------------------------------------------------
# v0.12.0 — TournamentPhaseConfig.num_branches: int = 1, range [1,5]
# ---------------------------------------------------------------------------


def test_num_branches_default_1() -> None:
    """v0.12.0: ``num_branches`` defaults to 1 (single-branch — no fan-out).

    The plan-tournament default in :mod:`config.defaults` overrides this to
    3 for parallel-branch fan-out per the v0.12.0 user-locked-in design.
    """
    from config.schema import TournamentPhaseConfig

    cfg = TournamentPhaseConfig(
        enabled=True, num_judges=5, convergence_k=2, max_rounds=15
    )
    assert cfg.num_branches == 1
    reloaded = TournamentPhaseConfig.model_validate(cfg.model_dump())
    assert reloaded.num_branches == 1


@pytest.mark.parametrize("n", [2, 3, 4, 5])
def test_num_branches_accepts_2_through_5(n: int) -> None:
    """``num_branches`` accepts any integer in ``[2, 5]`` (the cost-bounded
    fan-out range; 5 is the hard ceiling for branch concurrency)."""
    from config.schema import TournamentPhaseConfig

    cfg = TournamentPhaseConfig(
        enabled=True,
        num_judges=5,
        convergence_k=2,
        max_rounds=15,
        num_branches=n,
    )
    assert cfg.num_branches == n
    reloaded = TournamentPhaseConfig.model_validate(cfg.model_dump())
    assert reloaded.num_branches == n


def test_num_branches_negative_rejected() -> None:
    """Negative ``num_branches`` is rejected (validation failure)."""
    from config.schema import TournamentPhaseConfig

    with pytest.raises(ValidationError):
        TournamentPhaseConfig(
            enabled=True,
            num_judges=5,
            convergence_k=2,
            max_rounds=15,
            num_branches=-1,
        )


def test_num_branches_zero_rejected() -> None:
    """``num_branches=0`` is rejected (must be ≥1; 1 is the disable-fan-out
    sentinel)."""
    from config.schema import TournamentPhaseConfig

    with pytest.raises(ValidationError):
        TournamentPhaseConfig(
            enabled=True,
            num_judges=5,
            convergence_k=2,
            max_rounds=15,
            num_branches=0,
        )


def test_num_branches_above_ceiling_rejected() -> None:
    """``num_branches=6`` is rejected (>5 ceiling)."""
    from config.schema import TournamentPhaseConfig

    with pytest.raises(ValidationError):
        TournamentPhaseConfig(
            enabled=True,
            num_judges=5,
            convergence_k=2,
            max_rounds=15,
            num_branches=6,
        )


def test_legacy_config_without_num_branches_loads() -> None:
    """A pre-v0.12.0 config dict without ``num_branches`` validates and
    defaults the field to 1 (backward-compat)."""
    from config.schema import TournamentPhaseConfig

    legacy = TournamentPhaseConfig(
        enabled=True, num_judges=5, convergence_k=2, max_rounds=15
    ).model_dump()
    legacy.pop("num_branches", None)
    reloaded = TournamentPhaseConfig.model_validate(legacy)
    assert reloaded.num_branches == 1
