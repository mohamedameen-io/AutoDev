"""v0.14.0 architect prompt — EDIT SCOPE + INVESTIGATION PHASE PATHS sections.

Asserts the architect prompt documents (a) the EDIT_SCOPE block convention
that downstream agents enforce, and (b) the path convention for
investigation/notes work (git-tracked ``notes/``/``docs/``, NOT the
gitignored ``.autodev/notes/``).
"""

from __future__ import annotations

from pathlib import Path

PROMPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "agents"
    / "prompts"
    / "architect.md"
)


def _read() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def test_architect_prompt_documents_edit_scope_section() -> None:
    """The architect prompt must explain the ``EDIT_SCOPE:`` block."""
    body = _read()
    assert "EDIT SCOPE" in body or "EDIT_SCOPE" in body
    # And must mention the block syntax architects emit.
    assert "EDIT_SCOPE:" in body


def test_architect_prompt_documents_notes_path_convention() -> None:
    """The architect prompt must steer investigation tasks at git-tracked
    ``notes/``/``docs/`` (not the gitignored ``.autodev/notes/``)."""
    body = _read()
    assert "INVESTIGATION" in body
    # Must call out that .autodev/notes/ is gitignored / invisible.
    assert ".autodev/notes" in body
    # Positive guidance — the git-tracked alternative.
    assert "notes/" in body or "docs/" in body
