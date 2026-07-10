"""Backwards-compatibility pin for the v0.32.0 :class:`TestEvidence`
schema additions (Phase 3, Gap C).

Validates two contracts that protect operators upgrading from v0.31.x:

  1. **Read** — a v0.31.x-style TestEvidence dict on disk (without any
     of the new ``diagnosis`` / ``runner_returncode`` /
     ``tests_collected`` / ``collection_error`` / ``runner_stderr_tail``
     fields) deserialises cleanly with all new fields ``None``.
  2. **Write** — a freshly serialised v0.32.0-style TestEvidence
     contains all five new fields.

The contract matters because evidence files outlive the orchestrator
process — a v0.32.0 binary may resume against a worktree whose
``.autodev/evidence/<task>-test.json`` was written by v0.31.x.
"""

from __future__ import annotations

from state.schemas import TestEvidence


def _v031_evidence_payload() -> dict:
    """Return a TestEvidence dict shaped exactly like a v0.31.x write.

    Mirrors the field set in v0.31.x's ``TestEvidence`` (no diagnosis,
    no runner_returncode, no tests_collected, no collection_error,
    no runner_stderr_tail). Pinning the literal field set here so a
    future schema-shape regression is caught before it reaches users.
    """
    return {
        "kind": "test",
        "task_id": "1.1",
        "passed": 3,
        "failed": 0,
        "total": 3,
        "output_text": "RESULTS: passed=3 failed=0 total=3\n",
        "coverage_pct": 92.5,
        "raw_response": "ran pytest\nRESULTS: passed=3 failed=0 total=3\n",
    }


def test_v031_evidence_deserialises_with_diagnosis_none() -> None:
    """A v0.31.x dict on disk loads with all v0.32.0 fields ``None``.

    This is the operator-upgrade contract: nobody loses access to
    historical evidence files because Phase 3 added fields.
    """
    payload = _v031_evidence_payload()
    ev = TestEvidence.model_validate(payload)

    # Existing fields preserved verbatim.
    assert ev.passed == 3
    assert ev.failed == 0
    assert ev.total == 3
    assert ev.coverage_pct == 92.5
    assert ev.raw_response is not None and "RESULTS" in ev.raw_response

    # New v0.32.0 fields all default to None — the "no diagnosis
    # information available" sentinel.
    assert ev.diagnosis is None
    assert ev.runner_returncode is None
    assert ev.tests_collected is None
    assert ev.collection_error is None
    assert ev.runner_stderr_tail is None

    # WS1: the two new dispatch-layer fields also default to None so a
    # pre-WS1 evidence file (which never carried them) deserialises cleanly.
    assert ev.agent_subtype is None
    assert ev.agent_error is None


def test_v032_evidence_serialises_with_new_fields() -> None:
    """A freshly serialised v0.32.0 evidence carries all five new fields.

    ``model_dump`` must include the new keys (even when they are
    ``None``) so downstream tooling can rely on a stable on-disk
    schema rather than treating absent keys as unknown.
    """
    ev = TestEvidence(
        task_id="1.1",
        passed=0,
        failed=0,
        total=0,
        output_text="ERROR collecting tests/foo.py: ImportError",
        diagnosis="collection_failed",
        runner_returncode=2,
        tests_collected=0,
        collection_error="ImportError: No module named 'missing_dep'",
        runner_stderr_tail=(
            "================ ERRORS ================\n"
            "ERROR tests/foo.py - ImportError: missing_dep\n"
        ),
    )
    dumped = ev.model_dump()

    # All five new keys appear in the dict.
    for key in (
        "diagnosis",
        "runner_returncode",
        "tests_collected",
        "collection_error",
        "runner_stderr_tail",
    ):
        assert key in dumped, f"missing v0.32.0 field {key!r} in serialised dict"

    # Round-trip through model_validate preserves the values.
    rehydrated = TestEvidence.model_validate(dumped)
    assert rehydrated.diagnosis == "collection_failed"
    assert rehydrated.runner_returncode == 2
    assert rehydrated.tests_collected == 0
    assert rehydrated.collection_error is not None
    assert rehydrated.runner_stderr_tail is not None
    assert "ImportError" in rehydrated.runner_stderr_tail


def test_v032_evidence_diagnosis_literal_validates() -> None:
    """The ``diagnosis`` Literal accepts each documented value (incl. WS1's
    additive ``turn_budget_exhausted``)."""
    for diag in (
        "ok",
        "no_tests_found",
        "collection_failed",
        "runtime_crash",
        "capture_failed",
        "turn_budget_exhausted",
        "no_signal",
    ):
        ev = TestEvidence(task_id="1.1", diagnosis=diag)
        assert ev.diagnosis == diag


def test_ws1_agent_dispatch_fields_round_trip() -> None:
    """WS1: ``agent_subtype`` / ``agent_error`` serialise and round-trip, and
    the new ``turn_budget_exhausted`` diagnosis persists alongside them."""
    ev = TestEvidence(
        task_id="1.1",
        passed=0,
        failed=0,
        total=0,
        output_text="",
        diagnosis="turn_budget_exhausted",
        agent_subtype="error_max_turns",
        agent_error="claude exited 1: Reached maximum number of turns (8)",
    )
    dumped = ev.model_dump()
    assert "agent_subtype" in dumped
    assert "agent_error" in dumped

    rehydrated = TestEvidence.model_validate(dumped)
    assert rehydrated.diagnosis == "turn_budget_exhausted"
    assert rehydrated.agent_subtype == "error_max_turns"
    assert rehydrated.agent_error is not None
    assert "maximum number of turns" in rehydrated.agent_error


def test_v032_evidence_diagnosis_rejects_unknown_value() -> None:
    """Unknown ``diagnosis`` values are rejected by the Literal type.

    Pins the schema so a future contributor cannot silently extend
    the taxonomy without updating the ``Literal`` (and the classifier
    in lockstep).
    """
    import pytest as _pt
    from pydantic import ValidationError

    with _pt.raises(ValidationError):
        TestEvidence(task_id="1.1", diagnosis="bogus_diag")  # type: ignore[arg-type]
