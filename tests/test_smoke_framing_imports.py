"""Phase 0 smoke test: framing modules import; prompts strip frontmatter.

Temporary scaffolding — removable in Phase 1. The richer prompt-load assertions
move to ``test_agents_registry.py`` in Phase 5 (where the bodies legitimately
contain ``name:`` lines in the approaches contract, so the ``"name:"`` check here
is intentionally scoped to the Phase-0 placeholder bodies only).
"""

from __future__ import annotations

from agents import load_prompt


def test_framing_modules_import() -> None:
    import orchestrator.framing_phase  # noqa: F401
    import orchestrator.framing_signals  # noqa: F401


def test_framing_prompt_loads_strips_frontmatter() -> None:
    for role in ("framing", "altitude_judge"):
        body = load_prompt(role)
        # A missing closing ``---`` would ship the frontmatter (which contains
        # ``name:``) into the body; its absence proves both delimiters are present.
        assert "name:" not in body
        assert body.strip()
