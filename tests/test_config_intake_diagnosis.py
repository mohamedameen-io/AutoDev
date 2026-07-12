"""IntakePhaseConfig / DiagnosisPhaseConfig + v0.41.0 defaults tests.

Phase 0 (v0.41.0 Foundation): mirrors ``tests/test_config_framing_backcompat.py``
and ``tests/test_config_defaults.py``. Covers:
  - the two new phase-config factories + their defaults,
  - ``AutodevConfig`` exposing ``.intake`` / ``.diagnosis`` (on-by-default),
  - on-disk back-compat (legacy config missing the new fields still validates),
  - the A1 (reviewer) and A4 (critic_t / synthesizer) budget bumps,
  - the three new specialist roles in the denylist + agent-model defaults.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from config.defaults import (
    _AGENT_MAX_TURNS,
    _AGENT_MODEL_DEFAULTS,
    default_config,
)
from config.schema import (
    REQUIRED_AGENT_ROLES,
    AutodevConfig,
    DiagnosisPhaseConfig,
    IntakePhaseConfig,
    _default_diagnosis_cfg,
    _default_intake_cfg,
)


# ---------------------------------------------------------------------------
# Factory defaults
# ---------------------------------------------------------------------------


def test_default_intake_cfg_matches_factory() -> None:
    cfg = _default_intake_cfg()
    assert cfg.enabled is True
    assert cfg.on_unanswered == "assume_defaults"
    assert cfg.sources == ["repo", "github", "jira"]
    assert cfg.max_questions == 4
    assert cfg.exclude_globs == []
    assert cfg.reuse_explorer_evidence is True


def test_default_diagnosis_cfg_matches_factory() -> None:
    cfg = _default_diagnosis_cfg()
    assert cfg.enabled is True
    assert cfg.bug_only is True
    assert cfg.on_no_live_loop == "synthetic_plus_artifact"
    assert cfg.max_hypotheses == 5
    assert cfg.require_loop_to_plan is True


# ---------------------------------------------------------------------------
# AutodevConfig default exposure (on-by-default)
# ---------------------------------------------------------------------------


def test_default_config_intake_enabled() -> None:
    cfg = default_config()
    assert cfg.intake.enabled is True
    assert cfg.intake.on_unanswered == "assume_defaults"
    assert cfg.intake.sources == ["repo", "github", "jira"]


def test_default_config_diagnosis_enabled() -> None:
    cfg = default_config()
    assert cfg.diagnosis.enabled is True
    assert cfg.diagnosis.bug_only is True
    assert cfg.diagnosis.on_no_live_loop == "synthetic_plus_artifact"


def test_bare_autodev_config_default_factory_intake_diagnosis() -> None:
    """``AutodevConfig`` (constructed via the field default_factories, not the
    explicit ``default_config()`` constructor) also exposes on-by-default phases."""
    base = default_config().model_dump(mode="json")
    del base["intake"]
    del base["diagnosis"]
    cfg = AutodevConfig.model_validate(base)
    assert cfg.intake.enabled is True
    assert cfg.diagnosis.enabled is True


# ---------------------------------------------------------------------------
# On-disk back-compat (legacy config omits the new fields)
# ---------------------------------------------------------------------------


def test_legacy_config_missing_intake_field_validates() -> None:
    base = default_config().model_dump(mode="json")
    del base["intake"]
    cfg = AutodevConfig.model_validate(base)
    assert cfg.intake.enabled is True
    assert cfg.intake.max_questions == 4


def test_legacy_config_missing_diagnosis_field_validates() -> None:
    base = default_config().model_dump(mode="json")
    del base["diagnosis"]
    cfg = AutodevConfig.model_validate(base)
    assert cfg.diagnosis.enabled is True
    assert cfg.diagnosis.bug_only is True


def test_explicit_intake_overrides_applied() -> None:
    base = default_config().model_dump(mode="json")
    base["intake"]["on_unanswered"] = "block"
    cfg = AutodevConfig.model_validate(base)
    assert cfg.intake.on_unanswered == "block"


# ---------------------------------------------------------------------------
# Strictness (extra="forbid") + Literal / bound enforcement
# ---------------------------------------------------------------------------


def test_intake_phase_config_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        IntakePhaseConfig(enabled=True, bogus=1)  # type: ignore[call-arg]


def test_intake_on_unanswered_literal_enforced() -> None:
    with pytest.raises(ValidationError):
        IntakePhaseConfig(enabled=True, on_unanswered="whatever")  # type: ignore[arg-type]


def test_intake_max_questions_bounds() -> None:
    with pytest.raises(ValidationError):
        IntakePhaseConfig(enabled=True, max_questions=0)
    with pytest.raises(ValidationError):
        IntakePhaseConfig(enabled=True, max_questions=5)


def test_diagnosis_phase_config_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        DiagnosisPhaseConfig(enabled=True, bogus=1)  # type: ignore[call-arg]


def test_diagnosis_on_no_live_loop_literal_enforced() -> None:
    with pytest.raises(ValidationError):
        DiagnosisPhaseConfig(enabled=True, on_no_live_loop="ship_it")  # type: ignore[arg-type]


def test_diagnosis_max_hypotheses_bounds() -> None:
    with pytest.raises(ValidationError):
        DiagnosisPhaseConfig(enabled=True, max_hypotheses=2)
    with pytest.raises(ValidationError):
        DiagnosisPhaseConfig(enabled=True, max_hypotheses=6)


# ---------------------------------------------------------------------------
# A1 / A4 budget bumps
# ---------------------------------------------------------------------------


def test_reviewer_budget_bumped_to_8() -> None:
    """A1: reviewer max_turns 5 → 8 (belt-and-suspenders; the robust fix is
    diff-scoping handled elsewhere)."""
    assert _AGENT_MAX_TURNS["reviewer"] == 8
    assert default_config().agents["reviewer"].max_turns == 8


def test_test_engineer_budget_bumped_to_12() -> None:
    """WS-2a (slice4 forensic): test_engineer max_turns 8 → 12. The prior
    WS1 5→8 bump (same symptom) was "not enough" — 8 is structurally
    insufficient for the mandated write+run+iterate workload (crippled 9/10
    in the forensic). 12 = ceil(8 × 1.5), the budget-escalation ladder's
    attempt-1 rung promoted to the floor. The robust complement — a bounded
    prompt workload — ships in test_engineer.md."""
    assert _AGENT_MAX_TURNS["test_engineer"] == 12
    assert default_config().agents["test_engineer"].max_turns == 12


def test_critic_t_budget_bumped_to_6() -> None:
    """A4: critic_t max_turns 1 → 6."""
    assert _AGENT_MAX_TURNS["critic_t"] == 6
    assert default_config().agents["critic_t"].max_turns == 6


def test_synthesizer_budget_bumped_to_6() -> None:
    """A4: synthesizer max_turns 1 → 6."""
    assert _AGENT_MAX_TURNS["synthesizer"] == 6
    assert default_config().agents["synthesizer"].max_turns == 6


# ---------------------------------------------------------------------------
# New specialist roles: agent-model defaults + denylist + not-required
# ---------------------------------------------------------------------------


def test_new_roles_in_agent_model_defaults() -> None:
    for role in ("intake_enricher", "intake_clarifier", "diagnostician"):
        assert role in _AGENT_MODEL_DEFAULTS
        assert role in _AGENT_MAX_TURNS
    agents = default_config().agents
    assert "intake_enricher" in agents
    assert "intake_clarifier" in agents
    assert "diagnostician" in agents


def test_new_roles_in_denylist() -> None:
    dl = default_config().knowledge.denylist_roles
    assert "intake_enricher" in dl
    assert "intake_clarifier" in dl
    assert "diagnostician" in dl


def test_new_roles_not_in_required_roles() -> None:
    assert "intake_enricher" not in REQUIRED_AGENT_ROLES
    assert "intake_clarifier" not in REQUIRED_AGENT_ROLES
    assert "diagnostician" not in REQUIRED_AGENT_ROLES
