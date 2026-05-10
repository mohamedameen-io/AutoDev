"""v0.22.0 Phase 3: ``reviewer.md`` MINIMALITY CHECKLIST surface.

Pins the section's surface so future edits / accidental deletions are
caught at test time. Asserts the closed smell vocabulary, the
``@bloatware`` line format, and the Liu et al. citation are all present.
"""

from __future__ import annotations

from agents import load_prompt


_CLOSED_SMELL_VOCABULARY = (
    "long_method",
    "duplicate_code",
    "dead_code",
    "feature_envy",
    "speculative_generality",
    "shotgun_surgery",
    "primitive_obsession",
    "complex_conditional",
    "large_class",
)


def test_reviewer_prompt_includes_minimality_checklist_section() -> None:
    text = load_prompt("reviewer")
    assert "MINIMALITY CHECKLIST" in text


def test_reviewer_prompt_includes_bloatware_finding_format() -> None:
    text = load_prompt("reviewer")
    assert "@bloatware" in text


def test_reviewer_prompt_lists_all_closed_vocab_smells() -> None:
    text = load_prompt("reviewer")
    for smell in _CLOSED_SMELL_VOCABULARY:
        assert smell in text, f"closed-vocab smell {smell!r} missing from reviewer.md"


def test_reviewer_prompt_cites_liu_et_al_5_5x_finding() -> None:
    """The "why" preamble cites Liu et al.'s 5.5× recognition jump.

    Accept either Unicode multiplication sign (5.5×) or ASCII `5.5x` so
    formatting drift in the source doesn't break the test.
    """
    text = load_prompt("reviewer")
    assert ("5.5×" in text) or ("5.5x" in text)
