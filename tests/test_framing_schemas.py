"""SolutionApproach / FramingEvidence schema tests (Phase 1)."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from state.schemas import Evidence, FramingEvidence, SolutionApproach


def _approach(**overrides: object) -> SolutionApproach:
    base: dict[str, object] = dict(
        name="trim",
        altitude="local_patch",
        summary="trim the tool observation",
        eliminates_failure_class=False,
        primary_tradeoff="fast but only bounds the failure",
        primary_risk="recurs at the same seam",
        integration_surface=["src/foo.py"],
        est_blast_radius="single function",
    )
    base.update(overrides)
    return SolutionApproach(**base)  # type: ignore[arg-type]


def test_solution_approach_round_trip() -> None:
    sa = _approach(altitude="design_fix", eliminates_failure_class=True)
    dumped = sa.model_dump(mode="json")
    sa2 = SolutionApproach.model_validate(dumped)
    assert sa2.altitude == "design_fix"
    assert sa2.eliminates_failure_class is True
    assert sa2 == sa


def test_solution_approach_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        _approach(bogus_field="x")


def test_solution_approach_altitude_literal() -> None:
    with pytest.raises(ValidationError):
        _approach(altitude="invalid")


def _framing_ev(**overrides: object) -> FramingEvidence:
    base: dict[str, object] = dict(
        task_id="plan-framing",
        classification="realized_design_failure",
        confidence=0.85,
        hypothesis_challenged="user said trim; it is a design failure",
        signals_fired=["recurrence_at_seam"],
        approaches=[_approach()],
        chosen_approach_name="trim",
        altitude_rationale="eliminates the class",
    )
    base.update(overrides)
    return FramingEvidence(**base)  # type: ignore[arg-type]


def test_framing_evidence_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        _framing_ev(unknown_field="x")


def test_framing_evidence_confidence_bounds() -> None:
    with pytest.raises(ValidationError):
        _framing_ev(confidence=1.5)
    with pytest.raises(ValidationError):
        _framing_ev(confidence=-0.1)


def test_framing_evidence_discriminator_routing() -> None:
    payload = {
        "kind": "framing",
        "task_id": "plan-framing",
        "classification": "local_defect",
        "confidence": 0.0,
        "hypothesis_challenged": "h",
    }
    ev = TypeAdapter(Evidence).validate_python(payload)
    assert isinstance(ev, FramingEvidence)
    assert ev.classification == "local_defect"
