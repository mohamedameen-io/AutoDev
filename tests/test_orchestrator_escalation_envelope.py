"""v0.27 Phase 7 (audit §7): ESCALATE: parser unit tests.

Contract spec lives in :mod:`orchestrator.escalation_envelope`. The
tests cover the four predicates the parser must enforce:

  1. Exact ``ESCALATE:`` prefix at the start of the response → match.
  2. Prefix mid-paragraph → no match (avoids accidental escalations
     when an LLM dumps the autonomy-clause text verbatim).
  3. Whitespace before the prefix tolerated (LLMs sometimes emit a
     leading ``\\n``).
  4. The reason captures everything after the colon on the first
     line; trailing lines become ``context`` for the consult.
"""

from __future__ import annotations

from orchestrator.escalation_envelope import (
    EscalationEnvelope,
    parse_escalation_line,
)


def test_returns_none_for_empty_input() -> None:
    assert parse_escalation_line("") is None
    assert parse_escalation_line("   ") is None
    assert parse_escalation_line(None) is None  # type: ignore[arg-type]


def test_returns_none_when_prefix_is_in_prose() -> None:
    raw = (
        "Hello, here's my analysis.\n"
        "The spec is unclear. ESCALATE: I might say if blocked,\n"
        "but I'm not blocked yet, so continuing.\n"
    )
    assert parse_escalation_line(raw) is None


def test_returns_envelope_for_clean_first_line() -> None:
    raw = "ESCALATE: the spec is ambiguous about retry semantics"
    env = parse_escalation_line(raw)
    assert isinstance(env, EscalationEnvelope)
    assert env.reason == "the spec is ambiguous about retry semantics"
    assert env.context == ""
    assert env.raw_response == raw


def test_tolerates_leading_whitespace_and_newlines() -> None:
    raw = "\n\n  ESCALATE: missing dependency\n"
    env = parse_escalation_line(raw)
    assert env is not None
    assert env.reason == "missing dependency"


def test_captures_trailing_lines_as_context() -> None:
    raw = (
        "ESCALATE: spec contradicts the validator\n"
        "Detail: the spec says X but the path validator rejects X "
        "for reason Y. Need clarification on whether to relax the "
        "validator or rewrite the spec."
    )
    env = parse_escalation_line(raw)
    assert env is not None
    assert env.reason == "spec contradicts the validator"
    assert "Detail: the spec says X" in env.context
    assert "rewrite the spec" in env.context


def test_envelope_is_frozen_dataclass() -> None:
    """Callers can safely cache the envelope."""
    import pytest

    env = parse_escalation_line("ESCALATE: foo")
    assert env is not None
    with pytest.raises(Exception):
        env.reason = "other"  # type: ignore[misc]


def test_lowercase_prefix_does_not_match() -> None:
    """The contract is case-sensitive — ``escalate:`` is prose,
    only ``ESCALATE:`` is the protocol signal."""
    raw = "escalate: looks the same but isn't the signal"
    assert parse_escalation_line(raw) is None


def test_prefix_must_be_followed_by_colon() -> None:
    raw = "ESCALATE this might be misread but lacks the colon"
    assert parse_escalation_line(raw) is None


def test_empty_reason_still_produces_envelope() -> None:
    """A bare ``ESCALATE:`` with no reason text is still a signal
    (the orchestrator can still route the call to consult); reason
    is the empty string and the consult has nothing extra to chew on."""
    env = parse_escalation_line("ESCALATE:")
    assert env is not None
    assert env.reason == ""
    assert env.context == ""
