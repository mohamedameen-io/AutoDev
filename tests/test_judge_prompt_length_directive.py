"""Tests for the v0.6.2 stronger MANDATORY length penalty in JUDGE_RANK_3_PROMPT.

The QNX run at v0.5.2 had judges 4/5-voting AB on a 400-line plan despite
the v0.4.0 passive-voice "prefer the shorter one" line. v0.6.2 rewrites
this directive in active-voice MANDATORY phrasing with a 1.3× threshold
and a worked example. These tests guard the prompt text shape so future
edits don't silently revert the directive.
"""

from __future__ import annotations

from tournament.prompts import JUDGE_RANK_3_PROMPT


def test_judge_rank_3_prompt_contains_mandatory_phrasing() -> None:
    """The active-voice directive must use MANDATORY, the 1.3× threshold,
    and the explicit "ranked LAST" outcome to compel judges to penalize bloat.
    """
    text = JUDGE_RANK_3_PROMPT
    assert "MANDATORY" in text, (
        "JUDGE_RANK_3_PROMPT must include the active-voice MANDATORY phrasing "
        "introduced in v0.6.2 (the v0.4.0 passive language was insufficient)."
    )
    # Accept either the typographic '×' character or the ASCII 'x'.
    assert ("1.3×" in text) or ("1.3x" in text), (
        "Directive must specify the 1.3× length threshold."
    )
    assert "ranked LAST" in text, (
        "Directive must explicitly require violators be ranked LAST."
    )


def test_judge_rank_3_prompt_contains_worked_example() -> None:
    """The worked example anchors the rule with concrete numbers (200 vs 350
    lines) and a unambiguous correct ranking (1, 3, 2). Without the example
    judges have historically interpreted "shorter wins" as a soft heuristic
    rather than a binding rule.
    """
    text = JUDGE_RANK_3_PROMPT
    assert "200 lines" in text, "Worked example must mention the 200-line proposal."
    assert "350 lines" in text, "Worked example must mention the 350-line proposal."
    # The example explicitly spells out the correct ranking 1, 3, 2.
    assert "1, 3, 2" in text, (
        "Worked example must state the correct ranking 1, 3, 2 so judges "
        "have no ambiguity about how to apply the rule."
    )


def test_judge_rank_3_prompt_keeps_format_directive() -> None:
    """Regression: the new MANDATORY block must not have displaced the
    pre-existing RANKING output format — both must coexist.
    """
    text = JUDGE_RANK_3_PROMPT
    assert "RANKING:" in text, (
        "The RANKING: output line must remain present after the v0.6.2 rewrite."
    )
    assert "[best], [second], [worst]" in text, (
        "The slot template must remain present after the v0.6.2 rewrite."
    )
