"""v0.22.0 Phase 4: explicit token-budget regression for minimality_judge.md.

Distinct from ``test_minimality_judge_prompt.py`` so a budget-only failure
shows up as its own line in the test report. ShortCoder Table III: long
specialist prompts blow up token spend; staying under 2500 keeps cohort
costs bounded.
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


def test_file_line_count_at_most_400() -> None:
    raw = _PROMPT_PATH.read_text(encoding="utf-8")
    n_lines = len(raw.splitlines())
    assert n_lines <= 400, (
        f"minimality_judge.md is {n_lines} lines (max 400). "
        f"Trim §6 exemplars first — they are the largest section."
    )


def test_loaded_token_estimate_at_most_2500() -> None:
    text = load_prompt("minimality_judge")
    est = len(text.split()) * 1.3
    assert est <= 2500, (
        f"minimality_judge.md is ~{est:.0f} estimated tokens (max 2500). "
        f"Per-judge call cost scales linearly — overspend hurts cohort latency."
    )
