"""Tests for v0.11.0 additions to ``critic_sounding_board.md``.

Specifically validates:

* The new ``## CONFLICT ESCALATION MODE`` section is present in the prompt.
* The three required ``RESOLUTION:`` directives are documented.
* The pre-existing SOUNDING_BOARD flow (the legacy Verdict format) is
  preserved — the new section is opt-in via the ``CONFLICT_CONTEXT:``
  marker so callers without that marker see the legacy behavior.
"""

from __future__ import annotations

from pathlib import Path

_PROMPT_PATH = (
    Path(__file__).parent.parent
    / "src"
    / "agents"
    / "prompts"
    / "critic_sounding_board.md"
)


def _load_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def test_critic_prompt_includes_conflict_escalation_section() -> None:
    """The new section must be present in the shipped prompt file."""
    text = _load_prompt()
    assert "## CONFLICT ESCALATION MODE" in text


def test_critic_prompt_documents_three_resolution_directives() -> None:
    """The three RESOLUTION directives must be documented in the prompt."""
    text = _load_prompt()
    assert "RESOLUTION: rebase-and-retry" in text
    assert "RESOLUTION: abandon-task" in text
    assert "RESOLUTION: rewrite" in text


def test_critic_prompt_describes_conflict_context_input_marker() -> None:
    """The prompt must tell the agent to look for the CONFLICT_CONTEXT: marker."""
    text = _load_prompt()
    assert "CONFLICT_CONTEXT:" in text


def test_critic_existing_escalation_flow_unchanged() -> None:
    """Regression: the pre-existing SOUNDING_BOARD verdict format
    (UNNECESSARY / REPHRASE / APPROVED / RESOLVE) is still documented.
    Without this, callers using the legacy critic-escalation path would
    see different behavior."""
    text = _load_prompt()
    for verdict in ("UNNECESSARY", "REPHRASE", "APPROVED", "RESOLVE"):
        assert verdict in text, f"missing legacy verdict: {verdict}"


def test_critic_prompt_includes_worked_examples() -> None:
    """The new section must include at least one worked example so the
    LLM has a concrete pattern to follow."""
    text = _load_prompt()
    # Worked example for the rebase-and-retry case.
    assert "Worked example" in text
