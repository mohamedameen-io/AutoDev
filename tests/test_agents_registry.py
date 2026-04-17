"""Tests for :mod:`src.agents` registry builder."""

from __future__ import annotations

import pytest

from agents import build_registry, load_prompt, render_prompt
from config.defaults import default_config
from config.schema import REQUIRED_AGENT_ROLES


def test_build_registry_has_all_required_roles() -> None:
    """Registry covers every required role — no more, no fewer."""
    specs = build_registry(default_config())
    assert set(specs.keys()) == set(REQUIRED_AGENT_ROLES)
    assert len(specs) == len(REQUIRED_AGENT_ROLES)


def test_registry_prompts_non_empty() -> None:
    """Every non-tournament role must have a non-empty prompt.

    Tournament roles (critic_t/author_b/synthesizer/judge) are allowed to be
    empty if Phase 5 isn't installed, but with the current repo Phase 5 is
    present so we assert non-empty on all roles.
    """
    specs = build_registry(default_config())
    for role, spec in specs.items():
        assert spec.prompt, f"role {role} has empty prompt"
        assert len(spec.prompt) > 20, (
            f"role {role} has suspiciously short prompt ({len(spec.prompt)} chars)"
        )


def test_registry_tools_match_tool_map() -> None:
    """Each spec's tools come from resolve_claude_tools(role)."""
    from agents.tool_map import resolve_claude_tools

    specs = build_registry(default_config())
    for role, spec in specs.items():
        assert spec.tools == resolve_claude_tools(role)


def test_registry_models_from_config() -> None:
    """Each spec's model is taken from cfg.agents[role].model."""
    cfg = default_config()
    specs = build_registry(cfg)
    for role, spec in specs.items():
        assert spec.model == cfg.agents[role].model


def test_render_prompt_substitutes_placeholders() -> None:
    """``{{KEY}}`` substrings are replaced; unknown keys are left alone."""
    raw = "Retry limit: {{QA_RETRY_LIMIT}}\nTools: {{TOOLS}}\nKeep: {{UNKNOWN}}"
    out = render_prompt(raw, {"QA_RETRY_LIMIT": "3", "TOOLS": "Read, Edit, Grep"})
    assert "Retry limit: 3" in out
    assert "Tools: Read, Edit, Grep" in out
    assert "Keep: {{UNKNOWN}}" in out


def test_render_prompt_empty_context() -> None:
    """Empty context leaves the raw prompt unchanged."""
    raw = "Hello {{WHO}}"
    assert render_prompt(raw, {}) == raw


def test_registry_qa_retry_limit_substituted() -> None:
    """The architect's {{QA_RETRY_LIMIT}} must resolve to the config value."""
    cfg = default_config()
    cfg.qa_retry_limit = 5
    specs = build_registry(cfg)
    architect = specs["architect"]
    assert "{{QA_RETRY_LIMIT}}" not in architect.prompt
    # The literal "5" should appear in the rendered prompt.
    assert "5" in architect.prompt


def test_registry_tools_placeholder_substituted() -> None:
    """The architect's {{TOOLS}} must resolve to a comma-joined tool list."""
    specs = build_registry(default_config())
    architect = specs["architect"]
    assert "{{TOOLS}}" not in architect.prompt
    # At least one of Claude's tool names should appear from substitution.
    assert "Read" in architect.prompt


def test_load_prompt_strips_frontmatter() -> None:
    """load_prompt returns only the body, not the YAML frontmatter."""
    body = load_prompt("developer")
    assert not body.startswith("---")
    assert "name: coder" not in body.splitlines()[0]


def test_build_registry_missing_role_raises() -> None:
    """Config missing a required role must fail fast."""
    cfg = default_config()
    del cfg.agents["developer"]
    with pytest.raises(ValueError, match="developer"):
        build_registry(cfg)


def test_tournament_prompts_loaded_when_available() -> None:
    """Tournament roles pick up prompts from tournament.prompts."""
    importorskip_reason = None
    try:
        from tournament import prompts as tp
    except ImportError:
        importorskip_reason = "tournament.prompts not available"

    if importorskip_reason:
        pytest.skip(importorskip_reason)

    specs = build_registry(default_config())
    assert specs["critic_t"].prompt == tp.CRITIC_SYSTEM
    assert specs["architect_b"].prompt == tp.ARCHITECT_B_SYSTEM
    assert specs["synthesizer"].prompt == tp.SYNTHESIZER_SYSTEM
    assert specs["judge"].prompt == tp.JUDGE_SYSTEM
