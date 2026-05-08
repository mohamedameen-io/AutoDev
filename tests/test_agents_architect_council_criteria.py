"""v0.18.0 C2: architect.md must include the COUNCIL CRITERIA section."""

from __future__ import annotations

from pathlib import Path


def _read_architect() -> str:
    p = Path(__file__).parent.parent / "src" / "agents" / "prompts" / "architect.md"
    return p.read_text(encoding="utf-8")


def test_architect_has_council_criteria_section() -> None:
    body = _read_architect()
    assert "## COUNCIL CRITERIA" in body


def test_architect_council_criteria_explains_veto_mode() -> None:
    body = _read_architect()
    section_start = body.index("## COUNCIL CRITERIA")
    section_end = body.index("## OUTPUT REQUIREMENT — PLAN COMPLEXITY", section_start)
    section = body[section_start:section_end]
    assert "voting_strategy=veto" in section
    assert "Acceptance:" in section


def test_architect_council_criteria_has_concrete_example() -> None:
    body = _read_architect()
    section_start = body.index("## COUNCIL CRITERIA")
    section_end = body.index("## OUTPUT REQUIREMENT — PLAN COMPLEXITY", section_start)
    section = body[section_start:section_end]
    # The example should include a recognizable code block + at least one
    # `- [ ]` checkbox criterion.
    assert "```" in section
    assert "- [ ]" in section
