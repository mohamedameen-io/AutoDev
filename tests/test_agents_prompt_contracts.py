"""v0.33.0 anchor-string contract tests for the architect prompt.

The architect prompt at ``src/agents/prompts/architect.md`` carries two
v0.33.0 anchors that downstream tooling (CI preflight greps) and
release retrospectives rely on. These tests fail loudly if either anchor
is renamed or dropped, so a refactor that breaks the contract never
ships silently.
"""

from __future__ import annotations

from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
_ARCHITECT_PROMPT = _REPO_ROOT / "src" / "agents" / "prompts" / "architect.md"


def _read_prompt() -> str:
    return _ARCHITECT_PROMPT.read_text(encoding="utf-8")


def test_architect_prompt_documents_new_prefix_rule() -> None:
    """A2: ``[new]`` rule anchor must be present so the validator and the
    architect agree on cross-task file-creation semantics."""
    body = _read_prompt()
    assert "[new] PREFIX RULE" in body, (
        "src/agents/prompts/architect.md missing the v0.33.0 anchor "
        "'[new] PREFIX RULE'."
    )


def test_architect_prompt_forbids_notes_md_deliverables() -> None:
    """A3: investigation-notes deliverables must be explicitly forbidden so
    architects stop emitting ``notes-*.md`` plan artifacts the phase-review
    diff cannot evaluate."""
    body = _read_prompt()
    assert "NO INVESTIGATION NOTES" in body, (
        "src/agents/prompts/architect.md missing the v0.33.0 anchor "
        "'NO INVESTIGATION NOTES'."
    )
