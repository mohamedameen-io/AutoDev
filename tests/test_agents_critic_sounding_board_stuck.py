"""v0.15.0: ``## STUCK RECOVERY MODE`` section in critic_sounding_board prompt.

Validates that:
* The new ``## STUCK RECOVERY MODE`` section is present after the existing
  ``## CONFLICT ESCALATION MODE`` section (so the structural pattern
  matches and the parser can rely on the same anchor).
* The three required ``RESOLUTION:`` directives (``refine`` / ``pivot`` /
  ``soft-blocker``) are documented.
* The ``STUCK_CONTEXT:`` input marker is documented (mirrors
  ``CONFLICT_CONTEXT:``).
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


def test_critic_prompt_includes_stuck_recovery_section() -> None:
    text = _load_prompt()
    assert "## STUCK RECOVERY MODE" in text


def test_stuck_recovery_section_appears_after_conflict_escalation_section() -> None:
    """STUCK RECOVERY MODE must follow CONFLICT ESCALATION MODE so the
    structural pattern matches across both gated modes."""
    text = _load_prompt()
    conflict_pos = text.find("## CONFLICT ESCALATION MODE")
    stuck_pos = text.find("## STUCK RECOVERY MODE")
    assert conflict_pos != -1, "missing CONFLICT ESCALATION MODE section"
    assert stuck_pos != -1, "missing STUCK RECOVERY MODE section"
    assert stuck_pos > conflict_pos


def test_stuck_recovery_documents_three_resolution_directives() -> None:
    text = _load_prompt()
    assert "RESOLUTION: refine" in text
    assert "RESOLUTION: pivot" in text
    assert "RESOLUTION: soft-blocker" in text


def test_stuck_recovery_describes_stuck_context_input_marker() -> None:
    text = _load_prompt()
    assert "STUCK_CONTEXT:" in text


def test_stuck_recovery_includes_worked_example() -> None:
    """At least one worked example is required so the LLM has a concrete
    pattern to mirror."""
    text = _load_prompt()
    # The conflict section already contains "Worked example"; check the
    # stuck section has its own example by counting occurrences.
    assert text.count("Worked example") >= 2


def test_existing_critic_modes_preserved() -> None:
    """Regression: legacy SOUNDING_BOARD verdicts AND the conflict-mode
    directives must still be present (we only ADD the new section)."""
    text = _load_prompt()
    for verdict in ("UNNECESSARY", "REPHRASE", "APPROVED", "RESOLVE"):
        assert verdict in text
    for resolution in ("RESOLUTION: rebase-and-retry", "RESOLUTION: abandon-task"):
        assert resolution in text
