"""v0.22.0 Phase 5: structural test for §6 EXEMPLARS in minimality_judge.md.

Asserts the prompt contains exactly 5 exemplars in the expected format and
that the ranking distribution covers both dominant directions (Lean > Verbose
for the obvious cases, Verbose > Lean for the deliberately ambiguous case
that teaches the judge to defer to correctness when lean is wrong).
"""

from __future__ import annotations

import re
from pathlib import Path

from agents import load_prompt


_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "agents"
    / "prompts"
    / "minimality_judge.md"
)


def _load_text() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def test_exactly_five_exemplar_markers_present() -> None:
    """Exactly five `**Exemplar N: ...**` headers must be present."""
    text = _load_text()
    markers = re.findall(r"\*\*Exemplar (\d+):", text)
    assert markers == ["1", "2", "3", "4", "5"], (
        f"expected exemplar markers 1..5 in order, got {markers}"
    )


def test_each_exemplar_has_required_fields() -> None:
    """Every exemplar block must have Verbose, Lean, Correct ranking, and Rationale."""
    text = _load_text()
    # Slice the file into per-exemplar blocks using the marker as delimiter.
    # The first split element is the prelude; subsequent are exemplars 1..N.
    parts = re.split(r"\*\*Exemplar (\d+):[^\n]*\*\*\n", text)
    # parts looks like [prelude, "1", body1, "2", body2, ...]
    bodies = parts[2::2]
    assert len(bodies) == 5, f"expected 5 exemplar bodies, got {len(bodies)}"
    for idx, body in enumerate(bodies, start=1):
        assert "Verbose" in body, f"Exemplar {idx} missing 'Verbose' field"
        assert "Lean" in body, f"Exemplar {idx} missing 'Lean' field"
        assert "Correct ranking:" in body, (
            f"Exemplar {idx} missing 'Correct ranking:' field"
        )
        assert "Rationale" in body, f"Exemplar {idx} missing 'Rationale' field"


def test_ranking_distribution_covers_both_directions() -> None:
    """At least one Lean > Verbose ranking AND one Verbose > Lean ranking
    must appear. The verbose-first case is the deliberately ambiguous
    exemplar that teaches the judge to defer to correctness when leaner is
    incorrect."""
    text = _load_text()
    has_lean_first = "Lean > Verbose" in text
    has_verbose_first = "Verbose > Lean" in text
    assert has_lean_first, (
        "expected at least one 'Lean > Verbose' ranking among the exemplars"
    )
    assert has_verbose_first, (
        "expected at least one 'Verbose > Lean' ranking (the ambiguous case "
        "that teaches deferral to correctness)"
    )


def test_loaded_prompt_preserves_exemplar_section() -> None:
    """``load_prompt('minimality_judge')`` (post-frontmatter strip) still
    contains the §6 header and all five exemplar markers — guards against
    regressions in the loader."""
    text = load_prompt("minimality_judge")
    assert "§6. EXEMPLARS" in text
    for idx in range(1, 6):
        assert f"**Exemplar {idx}:" in text, (
            f"loaded prompt missing Exemplar {idx} marker"
        )
