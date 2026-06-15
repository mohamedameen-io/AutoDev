"""Intake (ADR-0045) and Diagnosis (ADR-0046) evidence-schema tests.

Phase 0 (v0.41.0 Foundation): the new value models and ``_BaseEvidence``
subclasses must instantiate, round-trip, enforce ``extra="forbid"``, and route
through the ``Evidence`` discriminated union via the ``kind`` discriminator.
Mirrors ``tests/test_framing_schemas.py``.
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from state.schemas import (
    ClarifyingAnswer,
    ClarifyingQuestion,
    DiagnosisEvidence,
    Evidence,
    FeedbackLoop,
    GatheredFact,
    Hypothesis,
    IntakeEvidence,
    SpecGaps,
)


# ---------------------------------------------------------------------------
# Intake value models
# ---------------------------------------------------------------------------


def test_spec_gaps_ok_true_when_no_gaps() -> None:
    """``SpecGaps.ok`` is the back-compat boolean: True ⇔ no missing dimensions."""
    g = SpecGaps(ok=True)
    assert g.ok is True
    assert g.missing == []


def test_spec_gaps_reflects_gaps() -> None:
    g = SpecGaps(ok=False, missing=["acceptance", "scope"])
    assert g.ok is False
    assert g.missing == ["acceptance", "scope"]


def test_spec_gaps_round_trip() -> None:
    g = SpecGaps(ok=False, missing=["constraints", "touchpoints"])
    g2 = SpecGaps.model_validate(g.model_dump(mode="json"))
    assert g2 == g


def test_spec_gaps_missing_literal_enforced() -> None:
    with pytest.raises(ValidationError):
        SpecGaps(ok=False, missing=["bogus_dimension"])  # type: ignore[list-item]


def test_spec_gaps_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        SpecGaps(ok=True, surprise=1)  # type: ignore[call-arg]


def test_gathered_fact_round_trip() -> None:
    f = GatheredFact(source="github", ref="github:org/repo#199", summary="full bug")
    f2 = GatheredFact.model_validate(f.model_dump(mode="json"))
    assert f2 == f
    assert f2.source == "github"


def test_gathered_fact_source_literal_enforced() -> None:
    with pytest.raises(ValidationError):
        GatheredFact(source="slack", ref="x", summary="y")  # type: ignore[arg-type]


def test_clarifying_question_round_trip() -> None:
    q = ClarifyingQuestion(
        id="provider",
        question="How much latitude on the provider?",
        kind="constraint",
        options=["Stay on Mistral", "Swap allowed", "Let AutoDev decide"],
        recommended="Stay on Mistral",
    )
    q2 = ClarifyingQuestion.model_validate(q.model_dump(mode="json"))
    assert q2 == q


def test_clarifying_question_kind_literal_enforced() -> None:
    with pytest.raises(ValidationError):
        ClarifyingQuestion(
            id="x",
            question="q",
            kind="solution_strategy",  # type: ignore[arg-type]
            options=["a"],
            recommended="a",
        )


def test_clarifying_answer_round_trip() -> None:
    a = ClarifyingAnswer(
        question_id="provider", answer="Stay on Mistral", source="default_assumed"
    )
    a2 = ClarifyingAnswer.model_validate(a.model_dump(mode="json"))
    assert a2 == a
    assert a2.source == "default_assumed"


# ---------------------------------------------------------------------------
# IntakeEvidence
# ---------------------------------------------------------------------------


def _intake_ev(**overrides: object) -> IntakeEvidence:
    base: dict[str, object] = dict(
        task_id="plan-intake",
        raw_intent="429 after bloated fetch",
        gaps=SpecGaps(ok=False, missing=["acceptance"]),
        gathered=[GatheredFact(source="repo", ref="src/foo.py:12-20", summary="path")],
        enriched_spec="## Success criteria\n- de-amplify retries",
        questions=[],
        answers=[],
        assumptions=["provider defaulted to Mistral"],
        locked_spec_hash="abc123",
        sources_used=["repo", "github"],
        excluded_globs=["**/solution/**"],
    )
    base.update(overrides)
    return IntakeEvidence(**base)  # type: ignore[arg-type]


def test_intake_evidence_kind_is_intake() -> None:
    assert _intake_ev().kind == "intake"


def test_intake_evidence_round_trip() -> None:
    ev = _intake_ev()
    ev2 = IntakeEvidence.model_validate(ev.model_dump(mode="json"))
    assert ev2 == ev


def test_intake_evidence_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        _intake_ev(bogus_field="x")


def test_intake_evidence_routes_through_union() -> None:
    payload = _intake_ev().model_dump(mode="json")
    ev = TypeAdapter(Evidence).validate_python(payload)
    assert isinstance(ev, IntakeEvidence)
    assert ev.locked_spec_hash == "abc123"


# ---------------------------------------------------------------------------
# Diagnosis value models
# ---------------------------------------------------------------------------


def test_feedback_loop_round_trip() -> None:
    loop = FeedbackLoop(
        method="replay_trace",
        command="pytest tests/repro/test_429.py",
        fidelity="replay",
        deterministic=True,
        runtime_s=1.2,
    )
    loop2 = FeedbackLoop.model_validate(loop.model_dump(mode="json"))
    assert loop2 == loop


def test_feedback_loop_method_literal_enforced() -> None:
    with pytest.raises(ValidationError):
        FeedbackLoop(method="vibes", command="x", fidelity="synthetic")  # type: ignore[arg-type]


def test_feedback_loop_fidelity_literal_enforced() -> None:
    with pytest.raises(ValidationError):
        FeedbackLoop(method="failing_test", command="x", fidelity="real")  # type: ignore[arg-type]


def test_hypothesis_round_trip_and_default_status() -> None:
    h = Hypothesis(
        rank=1,
        statement="retry amplifies the oversized fetch",
        prediction="if we cap the fetch, the 429 disappears",
    )
    assert h.status == "untested"
    h2 = Hypothesis.model_validate(h.model_dump(mode="json"))
    assert h2 == h


# ---------------------------------------------------------------------------
# DiagnosisEvidence
# ---------------------------------------------------------------------------


def _diagnosis_ev(**overrides: object) -> DiagnosisEvidence:
    base: dict[str, object] = dict(
        task_id="plan-diagnosis",
        loop=FeedbackLoop(
            method="replay_trace", command="pytest ...", fidelity="replay"
        ),
        reproduced=True,
        symptom="HTTP 429 after a bloated observation fetch",
        hypotheses=[
            Hypothesis(rank=1, statement="retry amplifies", prediction="cap → gone")
        ],
        confirmed_cause="unbounded retry on oversized payload",
        seam="none",
        loop_fidelity="replay",
        live_repro_artifact="scripts/repro/mistral_429.py",
        recurrence_at_seam=False,
        no_correct_seam=True,
    )
    base.update(overrides)
    return DiagnosisEvidence(**base)  # type: ignore[arg-type]


def test_diagnosis_evidence_kind_is_diagnosis() -> None:
    assert _diagnosis_ev().kind == "diagnosis"


def test_diagnosis_evidence_round_trip() -> None:
    ev = _diagnosis_ev()
    ev2 = DiagnosisEvidence.model_validate(ev.model_dump(mode="json"))
    assert ev2 == ev


def test_diagnosis_evidence_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        _diagnosis_ev(bogus_field="x")


def test_diagnosis_evidence_seam_literal_enforced() -> None:
    with pytest.raises(ValidationError):
        _diagnosis_ev(seam="maybe")


def test_diagnosis_evidence_routes_through_union() -> None:
    payload = _diagnosis_ev().model_dump(mode="json")
    ev = TypeAdapter(Evidence).validate_python(payload)
    assert isinstance(ev, DiagnosisEvidence)
    assert ev.no_correct_seam is True
    assert ev.seam == "none"


def test_diagnosis_evidence_minimal_defaults() -> None:
    """Only ``task_id`` is required; the rest carry safe defaults (fail-safe
    degrade path can persist a near-empty diagnosis)."""
    ev = DiagnosisEvidence(task_id="plan-diagnosis")
    assert ev.loop is None
    assert ev.reproduced is False
    assert ev.seam == "unknown"
    assert ev.loop_fidelity == "none"
    assert ev.hypotheses == []
