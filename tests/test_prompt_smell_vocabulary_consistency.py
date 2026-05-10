"""v0.22.0 Phase 3+4: closed smell vocabulary consistency across prompts.

Asserts that every smell name in the closed vocabulary appears in the
prompt(s) where it is the source of truth — ``reviewer.md`` and
``minimality_judge.md``. ``critic.md`` is plan-time and only enumerates
the subset relevant to plan review, so the consistency check is per-file
and tolerates that subset framing.

Phase 3 lands the full vocabulary in reviewer.md. Phase 4 lands
``minimality_judge.md`` — that file is now REQUIRED (the v0.22.0 Phase 4
cohort default uses it).
"""

from __future__ import annotations

from pathlib import Path


_PROMPTS_DIR = (
    Path(__file__).resolve().parent.parent / "src" / "agents" / "prompts"
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


def test_each_smell_appears_in_at_least_one_prompt() -> None:
    """Each of the 9 closed-vocab smell names must appear in at least
    one of the participating prompt files (reviewer.md, critic.md, and
    minimality_judge.md).

    Phase 3 landed the full vocabulary in reviewer.md; critic.md
    enumerates only the plan-time subset. Phase 4 landed
    minimality_judge.md — it is now required (no ``Path.exists()`` guard).
    """
    candidate_files = ["reviewer.md", "critic.md", "minimality_judge.md"]
    # Defensive: enforce existence — a missing file would yield a
    # confusing FileNotFoundError mid-test rather than a clear failure.
    for name in candidate_files:
        assert (_PROMPTS_DIR / name).exists(), (
            f"required prompt file missing: {name}"
        )

    contents = {
        name: (_PROMPTS_DIR / name).read_text(encoding="utf-8")
        for name in candidate_files
    }
    for smell in _CLOSED_SMELL_VOCABULARY:
        present_in = [name for name, text in contents.items() if smell in text]
        assert present_in, (
            f"closed-vocab smell {smell!r} missing from all prompt files: "
            f"{candidate_files}"
        )


def test_minimality_judge_contains_full_closed_vocabulary() -> None:
    """minimality_judge.md is the second canonical source — must list all 9 smells."""
    text = (_PROMPTS_DIR / "minimality_judge.md").read_text(encoding="utf-8")
    for smell in _CLOSED_SMELL_VOCABULARY:
        assert smell in text, (
            f"closed-vocab smell {smell!r} missing from minimality_judge.md"
        )


def test_reviewer_prompt_contains_full_closed_vocabulary() -> None:
    """reviewer.md is the canonical source — it must list all 9 smells."""
    text = (_PROMPTS_DIR / "reviewer.md").read_text(encoding="utf-8")
    for smell in _CLOSED_SMELL_VOCABULARY:
        assert smell in text, (
            f"closed-vocab smell {smell!r} missing from canonical reviewer.md"
        )
