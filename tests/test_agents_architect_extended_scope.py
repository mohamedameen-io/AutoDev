"""v0.20.0 C2: architect prompt EXTENDED SCOPE JUSTIFICATION section sanity."""

from __future__ import annotations

from pathlib import Path


_ARCH_PROMPT = (
    Path(__file__).parent.parent / "src" / "agents" / "prompts" / "architect.md"
)


def test_architect_md_documents_extended_scope_justification_section() -> None:
    text = _ARCH_PROMPT.read_text()
    assert "## EXTENDED SCOPE JUSTIFICATION" in text
    assert "Extended-scope:" in text
    assert "Justification:" in text


def test_architect_md_mentions_critic_routing() -> None:
    """The section MUST tell the architect that critic_sounding_board reviews."""
    text = _ARCH_PROMPT.read_text()
    body = text.split("## EXTENDED SCOPE JUSTIFICATION", 1)[1]
    assert "critic_sounding_board" in body
    assert "RESOLUTION: approved-extended-scope" in body or "approved-extended-scope" in body


def test_critic_sounding_board_md_documents_extended_scope_review_mode() -> None:
    p = (
        Path(__file__).parent.parent
        / "src"
        / "agents"
        / "prompts"
        / "critic_sounding_board.md"
    )
    text = p.read_text()
    assert "EXTENDED SCOPE REVIEW MODE" in text
    assert "RESOLUTION: approved-extended-scope" in text
    assert "RESOLUTION: rejected-extended-scope" in text
