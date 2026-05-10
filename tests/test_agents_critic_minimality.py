"""v0.22.0 Phase 3: ``critic.md`` MINIMALITY plan dimension.

Pins dimension #8 of the PLAN ASSESSMENT DIMENSIONS list and the Liu et al.
citation that justifies the addition.
"""

from __future__ import annotations

from agents import load_prompt


def test_critic_prompt_includes_minimality_dimension() -> None:
    """Dimension 8 must appear under PLAN ASSESSMENT DIMENSIONS."""
    text = load_prompt("critic")
    assert "MINIMALITY" in text
    # Anchored to dimension #8 in the numbered list.
    assert "8. **MINIMALITY**" in text


def test_critic_prompt_cites_liu_et_al_5_5x_finding() -> None:
    text = load_prompt("critic")
    assert ("5.5×" in text) or ("5.5x" in text)
