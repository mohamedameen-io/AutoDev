"""v0.22.0 Phase 4: structural tests for the minimality_judge.md prompt.

Asserts the prompt loads cleanly via the agent registry, contains the
load-bearing IAG section, the verbatim Bohr directive, all 9 closed-vocab
smell names, and the output-format ``RANKING:`` token. Also enforces the
hard constraints from the plan: ≤400 lines and ≤2500 estimated tokens
(``len(text.split()) * 1.3`` rough proxy).
"""

from __future__ import annotations

from pathlib import Path

from agents import load_prompt


_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "agents"
    / "prompts"
    / "minimality_judge.md"
)

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


def test_load_prompt_returns_non_empty() -> None:
    """``load_prompt('minimality_judge')`` returns the .md body."""
    text = load_prompt("minimality_judge")
    assert text  # non-empty
    assert len(text) > 200  # at least non-trivial content


def test_frontmatter_strips_correctly() -> None:
    """The YAML frontmatter block is removed by ``_strip_frontmatter``."""
    text = load_prompt("minimality_judge")
    # The raw on-disk file starts with "---\n", but the loaded prompt must not.
    assert not text.startswith("---\n")
    assert "---\ndescription:" not in text


def test_iag_section_present() -> None:
    """The load-bearing Independent Answer Generation section is preserved."""
    text = load_prompt("minimality_judge")
    assert "INDEPENDENT ANSWER GENERATION" in text


def test_bohr_directive_verbatim() -> None:
    """Bohr's exact directive (Cohen's d = -7.84) appears verbatim."""
    text = load_prompt("minimality_judge")
    assert "I value minimal, functional code" in text


def test_all_nine_closed_vocab_smells_appear() -> None:
    """Every smell in the closed vocabulary appears in the prompt body."""
    text = load_prompt("minimality_judge")
    for smell in _CLOSED_SMELL_VOCABULARY:
        assert smell in text, f"closed-vocab smell {smell!r} missing"


def test_output_format_ranking_marker_present() -> None:
    """The ``RANKING:`` output-format marker is present (parser hook)."""
    text = load_prompt("minimality_judge")
    assert "RANKING:" in text


def test_file_at_most_400_lines() -> None:
    """Hard constraint: total file ≤ 400 lines."""
    raw_lines = _PROMPT_PATH.read_text(encoding="utf-8").splitlines()
    assert len(raw_lines) <= 400, (
        f"minimality_judge.md is {len(raw_lines)} lines, max allowed is 400"
    )


def test_loaded_body_at_most_2500_estimated_tokens() -> None:
    """Hard constraint: ≤2500 estimated tokens (words × 1.3 rough proxy).

    The estimate is intentionally rough — it avoids dragging in a tokenizer
    dependency. ShortCoder Table III: long specialist prompts blow up token
    spend; staying under 2500 keeps cohort costs bounded.
    """
    text = load_prompt("minimality_judge")
    estimated_tokens = len(text.split()) * 1.3
    assert estimated_tokens <= 2500, (
        f"minimality_judge.md is ~{estimated_tokens:.0f} estimated tokens, "
        f"max allowed is 2500"
    )
