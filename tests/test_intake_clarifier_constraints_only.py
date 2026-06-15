"""Corpus audit: the intake_clarifier prompt carries the constraints-not-solutions
guard (ADR-0045 KD1 / ADR-0044 boundary, NFR4).

This is the static-artifact half of the merge gate — it asserts the *prompt*
hard-codes the boundary. (The runtime "0 solution-shaped questions over a
generated corpus" half lives with intake-core's question-validation pass.)
"""

from __future__ import annotations

import re

from agents import load_prompt


def _clarifier() -> str:
    return load_prompt("intake_clarifier")


def _norm(text: str) -> str:
    """Lowercased, whitespace-collapsed view so a wrapped phrase still matches."""
    return re.sub(r"\s+", " ", text.lower())


def test_clarifier_states_constraints_not_solutions_rule() -> None:
    text = _norm(_clarifier())
    # The load-bearing rule, stated explicitly.
    assert "constraints, not solutions" in text
    assert "ask only about constraints" in text
    assert "never about solutions" in text


def test_clarifier_forbids_enumerating_or_selecting_solutions() -> None:
    text = _norm(_clarifier())
    assert "enumerate or select solution strategies" in text
    # Explicit prohibition on the A/B solution-choice shape.
    assert "approach a or b" in text


def test_clarifier_leaves_altitude_to_framing() -> None:
    text = _norm(_clarifier())
    assert "framing" in text
    # The altitude decision is named as framing's job, not the clarifier's.
    assert "altitude" in text
    assert "pre-empt" in text or "preempt" in text


def test_clarifier_allows_altitude_latitude_preference_as_constraint() -> None:
    text = _norm(_clarifier())
    # KD1: a latitude PREFERENCE may be captured as a constraint.
    assert "altitude-latitude preference" in text
    assert "risk_latitude" in text


def test_clarifier_kinds_are_constraint_shaped_only() -> None:
    text = _clarifier()
    # The fixed, constraint-only kind set (matches the ClarifyingQuestion Literal).
    for kind in ("constraint", "environment", "done_bar", "risk_latitude", "compat"):
        assert kind in text
    # No solution/strategy-shaped kind is offered.
    lowered = text.lower()
    assert "solution_choice" not in lowered
    assert "approach_select" not in lowered


def test_clarifier_has_forbidden_examples() -> None:
    """The prompt teaches by example: at least one FORBIDDEN solution-shaped
    sample so the model learns the line, not just the rule."""
    text = _clarifier()
    assert "FORBIDDEN" in text
    lowered = _norm(text)
    # Canonical contamination examples from the ADR (refactor / fix-choice).
    assert "refactor" in lowered
    assert "trim the strings" in lowered or "artifact store" in lowered
