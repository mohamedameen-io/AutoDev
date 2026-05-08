"""v0.17.0 S3: ``judge_explorer.md`` prompt exists and follows conventions.

Pins the prompt's surface so future renames / accidental deletions are
caught at test time rather than at runtime when the orchestrator tries
to dispatch the role.
"""

from __future__ import annotations

from pathlib import Path


_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "agents"
    / "prompts"
    / "judge_explorer.md"
)


def test_judge_explorer_prompt_exists() -> None:
    assert _PROMPT_PATH.exists(), f"missing prompt: {_PROMPT_PATH}"


def test_judge_explorer_prompt_declares_findings_section() -> None:
    text = _PROMPT_PATH.read_text(encoding="utf-8")
    assert "FINDINGS:" in text
    # Recognized categories list.
    for cat in (
        "slop_pattern",
        "hallucinated_api",
        "lazy_abstraction",
        "cargo_cult",
        "spec_drift",
    ):
        assert cat in text, f"category {cat!r} missing from prompt"


def test_judge_explorer_prompt_emits_ranking_first() -> None:
    """Ranking must come BEFORE findings — the standard judge parser
    runs first and would crash on a leading ``FINDINGS:`` block."""
    text = _PROMPT_PATH.read_text(encoding="utf-8")
    ranking_idx = text.find("RANKING:")
    findings_idx = text.find("FINDINGS:")
    assert ranking_idx >= 0
    assert findings_idx >= 0
    assert ranking_idx < findings_idx
