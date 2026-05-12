"""v0.27 Phase 7 (audit §7): ESCALATE: line parser.

The shared autonomy clause (embedded in every role prompt) tells the
agent to emit a single ``ESCALATE: <reason>`` line as the first line
of its response when the request is genuinely under-specified or
contradicts a constraint. This module is the runtime parser the
orchestrator uses to detect that signal and route to the
architect-consult rung.

Contract:

  * The ``ESCALATE:`` prefix is recognised case-sensitively (the
    autonomy clause specifies exact spelling).
  * The prefix MUST be the first non-whitespace content on its own
    line at the very start of the response. A response that mentions
    ``ESCALATE:`` mid-paragraph is NOT an escalation — it's an
    accidental match (the autonomy clause itself contains that
    string).
  * Anything on the same line after the prefix is the ``reason``.
  * Anything after that line (newline-separated) is captured as
    ``context`` for the consult.
"""

from __future__ import annotations

from dataclasses import dataclass


_PREFIX = "ESCALATE:"


@dataclass(frozen=True)
class EscalationEnvelope:
    """Parsed shape of a successful escalation signal."""

    reason: str
    context: str
    raw_response: str


def parse_escalation_line(raw: str) -> EscalationEnvelope | None:
    """Return an :class:`EscalationEnvelope` when ``raw`` starts with
    the ESCALATE: prefix; otherwise ``None``.

    The contract is strict: the prefix must be the first non-whitespace
    content on its own first line. This avoids accidental matches when
    a role prompt or downstream output mentions the prefix in prose.

    Whitespace before the prefix is tolerated (handles ``\\n``-prefixed
    LLM outputs that start with a stray newline); whitespace between
    the colon and the reason is collapsed.
    """
    if not raw:
        return None

    stripped = raw.lstrip()
    if not stripped.startswith(_PREFIX):
        return None

    # Split the first line off the rest. ``splitlines`` strips the
    # delimiter so we don't have to think about \r\n.
    lines = stripped.splitlines()
    if not lines:
        return None
    first = lines[0]
    rest = "\n".join(lines[1:]).strip()

    reason = first[len(_PREFIX):].strip()
    return EscalationEnvelope(
        reason=reason,
        context=rest,
        raw_response=raw,
    )


__all__ = ["EscalationEnvelope", "parse_escalation_line"]
