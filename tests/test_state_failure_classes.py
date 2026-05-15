"""Tests for v0.32.0 Phase 1.3 — plan-time vs execute-time failure-class taxonomy.

Covers :mod:`state.failure_classes`. The taxonomy is the routing key
used by Phase 1.4's recovery tiers: a wrong classification on a
``PathValidationError`` recurrence would silently skip scope
degradation, so we exhaustively cover each branch the classifier
discriminates on.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from orchestrator.path_validator import PathValidationError
from orchestrator.plan_parser import PlanParseError
from state.failure_classes import (
    FailureClass,
    classify,
)


def test_classify_path_validation_recurrence() -> None:
    """``PathValidationError`` with recurrence >= 3 promotes to recurrence."""
    exc = PathValidationError("notes", "missing_on_disk")
    assert classify(exc, recurrence_count=3) == FailureClass.PathValidationRecurrence


def test_classify_path_validation_under_threshold_is_unknown() -> None:
    """Single-shot path validation failures fall through to Unknown so the
    legacy retry loop handles them; only recurrent shapes get specialised
    recovery."""
    exc = PathValidationError("notes", "missing_on_disk")
    assert classify(exc, recurrence_count=1) == FailureClass.Unknown
    assert classify(exc, recurrence_count=2) == FailureClass.Unknown


def test_classify_plan_parse_error_under_threshold() -> None:
    """A first-shot ``PlanParseError`` classifies as ``PlanParseError``."""
    exc = PlanParseError("missing title")
    assert classify(exc, recurrence_count=0) == FailureClass.PlanParseError


def test_classify_plan_parse_error_recurrence() -> None:
    """Three repeated ``PlanParseError`` failures promote to recurrence."""
    exc = PlanParseError("missing title")
    assert classify(exc, recurrence_count=3) == FailureClass.ParseErrorRecurrence


def test_classify_plan_structure_error() -> None:
    """A pydantic ``ValidationError`` (plan structure) routes to its branch."""

    class _Tiny(BaseModel):
        x: int

    try:
        _Tiny(x="not-an-int")  # type: ignore[arg-type]
    except ValidationError as exc:
        assert classify(exc) == FailureClass.PlanStructureError
    else:
        pytest.fail("ValidationError should have fired")


def test_classify_error_max_turns() -> None:
    """An ``AgentResult``-like object with ``subtype='error_max_turns'``
    classifies as the execute-time ``ErrorMaxTurns``."""

    class _FakeResult:
        subtype = "error_max_turns"

    assert classify(_FakeResult()) == FailureClass.ErrorMaxTurns


def test_classify_timeout_subtype() -> None:
    class _FakeResult:
        subtype = "timeout"

    assert classify(_FakeResult()) == FailureClass.Timeout


def test_classify_parse_error_subtype() -> None:
    class _FakeResult:
        subtype = "parse_error"

    assert classify(_FakeResult()) == FailureClass.ParseError


def test_classify_rate_limited_subtype() -> None:
    class _FakeResult:
        subtype = "rate_limited"

    assert classify(_FakeResult()) == FailureClass.RateLimited


def test_classify_auth_failed_subtype() -> None:
    class _FakeResult:
        subtype = "auth_failed"

    assert classify(_FakeResult()) == FailureClass.AuthFailed


def test_classify_unknown() -> None:
    """Unrecognised inputs return ``FailureClass.Unknown``."""
    assert classify(object()) == FailureClass.Unknown
    assert classify(None) == FailureClass.Unknown
    assert classify(RuntimeError("nope")) == FailureClass.Unknown


def test_classify_unknown_subtype_string() -> None:
    """An ``AgentResult``-like with an unknown subtype falls through to Unknown."""

    class _FakeResult:
        subtype = "transport_jitter"

    assert classify(_FakeResult()) == FailureClass.Unknown


def test_failure_class_values_are_strings() -> None:
    """Each enum value is a stable string for ledger payload serialisation."""
    for fc in FailureClass:
        assert isinstance(fc.value, str), fc
        assert fc.value, f"{fc} must have a non-empty value"


def test_classify_string_shorthand() -> None:
    """Bare strings matching an enum value round-trip through ``classify``."""
    assert classify("execute.error_max_turns") == FailureClass.ErrorMaxTurns
    assert (
        classify("plan.path_validation_recurrence")
        == FailureClass.PathValidationRecurrence
    )
    # An arbitrary bare string falls through to Unknown.
    assert classify("nope.not_a_class") == FailureClass.Unknown
