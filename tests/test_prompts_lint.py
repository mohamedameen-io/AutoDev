"""v0.27 Phase 7: lint guards on the 14 role prompts.

Two invariants this test enforces:

  1. Every role prompt embeds the shared autonomy clause. The clause
     lives at :mod:`src/agents/prompts/_source/_autonomy_clause.md` for
     reference; each role prompt embeds a verbatim copy with the
     marker ``# shared: _autonomy_clause.md — keep in sync``.

  2. No prompt contains forbid-list phrasing that re-introduces the
     human-loop assumption (e.g. ``please confirm``, ``let me know``,
     ``would you like``). These phrases cause role outputs to ask the
     orchestrator questions it cannot answer — burning a retry cycle
     per question.
"""

from __future__ import annotations

from pathlib import Path

import pytest


_PROMPTS_DIR = (
    Path(__file__).parent.parent / "src" / "agents" / "prompts"
)


def _list_role_prompts() -> list[Path]:
    """Every ``*.md`` under ``src/agents/prompts/`` except the
    ``_source/`` reference copies."""
    return sorted(
        p
        for p in _PROMPTS_DIR.glob("*.md")
        if not p.name.startswith("_")
    )


@pytest.mark.parametrize(
    "prompt_path", _list_role_prompts(), ids=lambda p: p.name
)
def test_role_prompt_embeds_autonomy_clause(prompt_path: Path) -> None:
    """Every role prompt embeds the v0.27 autonomy clause.

    The clause is recognised by its keep-in-sync marker comment so a
    future engineer who edits the clause body can find every embed
    via ``grep '_autonomy_clause.md' src/agents/prompts``.
    """
    text = prompt_path.read_text(encoding="utf-8")
    assert "_autonomy_clause.md — keep in sync" in text, (
        f"{prompt_path.name}: missing autonomy clause marker. "
        "Embed the clause from src/agents/prompts/_source/_autonomy_clause.md "
        "with the comment `<!-- shared: _autonomy_clause.md — keep in sync -->`."
    )
    # Spot-check that the clause body actually landed (not just the marker).
    assert "ESCALATE:" in text, (
        f"{prompt_path.name}: clause marker present but ESCALATE: directive missing."
    )
    assert "running unattended" in text, (
        f"{prompt_path.name}: clause marker present but body text missing."
    )


_FORBIDDEN_PHRASES: list[str] = [
    "please confirm",
    "let me know",
    "would you like",
    "do you want me to",
    "shall I proceed",
    "should I proceed",
    "any preference",
]


@pytest.mark.parametrize(
    "prompt_path", _list_role_prompts(), ids=lambda p: p.name
)
def test_role_prompt_has_no_human_loop_phrasing(prompt_path: Path) -> None:
    """Forbid the role-output anti-patterns that ask the orchestrator
    questions it cannot answer."""
    text = prompt_path.read_text(encoding="utf-8").lower()
    for phrase in _FORBIDDEN_PHRASES:
        assert phrase not in text, (
            f"{prompt_path.name}: contains forbidden phrasing {phrase!r}. "
            "Role prompts must not encourage human-loop dialogue; the "
            "orchestrator has no operator to answer questions."
        )


def test_autonomy_clause_source_file_exists() -> None:
    """The reference clause file is the source of truth — keep it in
    sync with each role prompt's embedded copy."""
    src = _PROMPTS_DIR / "_source" / "_autonomy_clause.md"
    assert src.exists(), f"missing reference clause at {src}"
    text = src.read_text(encoding="utf-8")
    assert "ESCALATE:" in text
    assert "running unattended" in text
