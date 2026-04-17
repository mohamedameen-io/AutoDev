"""Tests for src.config schema and loader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from config.defaults import default_config
from config.loader import expand_paths, load_config, save_config
from config.schema import REQUIRED_AGENT_ROLES, AutodevConfig
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


def test_unknown_top_level_field_rejected(tmp_path: Path) -> None:
    cfg = default_config()
    data = cfg.model_dump(mode="json")
    data["unexpected_field"] = "oops"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)
