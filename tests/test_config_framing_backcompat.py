"""FramingPhaseConfig / on-disk back-compat tests (Phase 1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from config.defaults import (
    _AGENT_MAX_TURNS,
    _AGENT_MODEL_DEFAULTS,
    default_config,
)
from config.schema import REQUIRED_AGENT_ROLES, AutodevConfig, FramingPhaseConfig


def test_legacy_config_missing_framing_field_validates() -> None:
    base = default_config().model_dump(mode="json")
    del base["framing"]
    cfg = AutodevConfig.model_validate(base)
    assert cfg.framing.enabled is True
    assert cfg.framing.design_smell_threshold == 0.7


def test_explicit_framing_overrides_applied() -> None:
    base = default_config().model_dump(mode="json")
    base["framing"]["design_smell_threshold"] = 0.5
    cfg = AutodevConfig.model_validate(base)
    assert cfg.framing.design_smell_threshold == 0.5


def test_framing_phase_config_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        FramingPhaseConfig(enabled=True, bogus=1)  # type: ignore[call-arg]


def test_framing_config_num_approaches_bounds() -> None:
    with pytest.raises(ValidationError):
        FramingPhaseConfig(enabled=True, num_approaches=1)
    with pytest.raises(ValidationError):
        FramingPhaseConfig(enabled=True, num_approaches=4)


def test_framing_config_threshold_bounds() -> None:
    with pytest.raises(ValidationError):
        FramingPhaseConfig(enabled=True, design_smell_threshold=1.5)
    with pytest.raises(ValidationError):
        FramingPhaseConfig(enabled=True, design_smell_threshold=-0.1)


def test_framing_config_panel_size_bounds() -> None:
    with pytest.raises(ValidationError):
        FramingPhaseConfig(enabled=True, altitude_judge_panel_size=0)
    with pytest.raises(ValidationError):
        FramingPhaseConfig(enabled=True, altitude_judge_panel_size=6)


def test_denylist_roles_includes_framing_and_altitude_judge() -> None:
    dl = default_config().knowledge.denylist_roles
    assert "framing" in dl
    assert "altitude_judge" in dl


def test_agent_defaults_include_framing_roles() -> None:
    assert "framing" in _AGENT_MODEL_DEFAULTS
    assert "altitude_judge" in _AGENT_MODEL_DEFAULTS
    assert "framing" in _AGENT_MAX_TURNS
    assert "altitude_judge" in _AGENT_MAX_TURNS
    agents = default_config().agents
    assert "framing" in agents
    assert "altitude_judge" in agents


def test_framing_roles_not_in_required_roles() -> None:
    assert "framing" not in REQUIRED_AGENT_ROLES
    assert "altitude_judge" not in REQUIRED_AGENT_ROLES
