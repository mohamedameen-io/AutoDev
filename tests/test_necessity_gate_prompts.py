"""Tests for the B1 necessity ladder in tournament prompts.

``ARCHITECT_B_SYSTEM`` and ``_ARCHITECT_B_PROMPT_IMPL`` must both contain
the necessity-ladder guidance so the tournament consultant refuses to
add unjustified dependencies or abstractions.

The *assembled* developer prompt (the actual string the coder runs with,
constructed in ``_CoderRunner.run`` and ``delegate()``) must also contain
the ladder rungs and the safety/security carve-out — that injection is the
core of the B1 spec-review fix.

Pattern follows ``tests/test_judge_prompt_length_directive.py``.
"""

from __future__ import annotations

from agents import load_prompt, render_prompt
from tournament.prompts import ARCHITECT_B_SYSTEM, build_developer_prompt


# ---------------------------------------------------------------------------
# Architect-B tests (existing)
# ---------------------------------------------------------------------------

def test_architect_b_system_contains_necessity_ladder_keywords() -> None:
    """ARCHITECT_B_SYSTEM must contain the necessity-ladder content.

    At minimum the ladder must reference: standard library, native
    platform/runtime/framework, existing (dep/code), dependency, and the
    safety/security carve-out.
    """
    text = ARCHITECT_B_SYSTEM.lower()
    assert "standard library" in text, (
        "ARCHITECT_B_SYSTEM must mention 'standard library' as rung 2 of the "
        "laziness ladder."
    )
    assert "platform" in text or "runtime" in text or "framework" in text, (
        "ARCHITECT_B_SYSTEM must contain the native platform/runtime/framework rung "
        "(rung 3 of the laziness ladder)."
    )
    assert "existing" in text, (
        "ARCHITECT_B_SYSTEM must reference 'existing' (project dep / platform)."
    )
    assert "dependency" in text or "dependencies" in text, (
        "ARCHITECT_B_SYSTEM must mention 'dependency' (rung 4 of the ladder)."
    )


def test_architect_b_system_contains_safety_carve_out() -> None:
    """The safety/security carve-out must be present so reviewers never skip it."""
    text = ARCHITECT_B_SYSTEM.lower()
    has_safety = "safety" in text or "security" in text
    assert has_safety, (
        "ARCHITECT_B_SYSTEM must contain the safety/security carve-out "
        "(safety, input validation, error handling, and security work are "
        "NEVER skipped by the necessity ladder)."
    )


def test_architect_b_prompt_impl_contains_necessity_ladder() -> None:
    """_ARCHITECT_B_PROMPT_IMPL must also contain the necessity-ladder content."""
    # Import lazily to avoid circular import issues at module level.
    from tournament.impl_tournament import _ARCHITECT_B_PROMPT_IMPL  # type: ignore[attr-defined]

    text = _ARCHITECT_B_PROMPT_IMPL.lower()
    assert "standard library" in text, (
        "_ARCHITECT_B_PROMPT_IMPL must mention 'standard library'."
    )
    assert "platform" in text or "runtime" in text or "framework" in text, (
        "_ARCHITECT_B_PROMPT_IMPL must contain the native platform/runtime/framework rung."
    )
    assert "existing" in text, (
        "_ARCHITECT_B_PROMPT_IMPL must reference 'existing'."
    )
    assert "dependency" in text or "dependencies" in text, (
        "_ARCHITECT_B_PROMPT_IMPL must mention 'dependency'."
    )


def test_architect_b_prompt_impl_contains_safety_carve_out() -> None:
    """_ARCHITECT_B_PROMPT_IMPL must include the safety/security carve-out."""
    from tournament.impl_tournament import _ARCHITECT_B_PROMPT_IMPL  # type: ignore[attr-defined]

    text = _ARCHITECT_B_PROMPT_IMPL.lower()
    has_safety = "safety" in text or "security" in text
    assert has_safety, (
        "_ARCHITECT_B_PROMPT_IMPL must contain the safety/security carve-out."
    )


# ---------------------------------------------------------------------------
# Assembled developer prompt tests (B1 spec-review fix)
#
# These tests verify the *actual assembled string* that the coder runs with
# (``developer_spec.prompt.strip() + NECESSITY_LADDER_GUIDANCE``), not just
# the constant. This is the test that was missing from the original B1 commit.
# ---------------------------------------------------------------------------

def _assembled_developer_prompt() -> str:
    """Reproduce the assembly performed in ``_CoderRunner.run`` and ``delegate()``.

    Both sites call ``build_developer_prompt(spec.prompt)`` where ``spec.prompt``
    is the rendered developer.md (with frontmatter stripped and ``{{...}}``
    placeholders substituted). Using the shared helper here ensures this test
    stays in sync with both production injection sites.
    """
    raw = load_prompt("developer")
    rendered = render_prompt(raw, {"QA_RETRY_LIMIT": "3", "TOOLS": "(none)"})
    return build_developer_prompt(rendered)


def test_assembled_developer_prompt_contains_necessity_ladder_rungs() -> None:
    """The assembled developer prompt must include all core ladder rungs.

    Verified rungs: standard library, native platform/runtime/framework,
    existing project dependency, and the rung for new code inline.
    This test FAILS before the B1 injection and passes after.
    """
    text = _assembled_developer_prompt().lower()
    assert "standard library" in text, (
        "Assembled developer prompt must contain 'standard library' rung."
    )
    assert "platform" in text or "runtime" in text or "framework" in text, (
        "Assembled developer prompt must contain the native platform/runtime/framework rung."
    )
    assert "existing" in text, (
        "Assembled developer prompt must reference 'existing' (project dep / code rung)."
    )
    assert "dependency" in text or "dependencies" in text, (
        "Assembled developer prompt must mention 'dependency' (new-dep rung)."
    )


def test_assembled_developer_prompt_contains_safety_carve_out() -> None:
    """The assembled developer prompt must contain the safety/security carve-out.

    This ensures safety / input-validation / error-handling work is never
    gated by the necessity ladder.
    This test FAILS before the B1 injection and passes after.
    """
    text = _assembled_developer_prompt().lower()
    has_safety = "safety" in text or "security" in text
    assert has_safety, (
        "Assembled developer prompt must contain the safety/security carve-out. "
        "Inject NECESSITY_LADDER_GUIDANCE when building the developer prompt."
    )


def test_assembled_developer_prompt_includes_never_skipped_clause() -> None:
    """The carve-out must state that safety work is NEVER skipped by the ladder."""
    text = _assembled_developer_prompt().lower()
    # The carve-out text contains "never" to make the skip prohibition unambiguous.
    assert "never" in text, (
        "Assembled developer prompt must contain 'NEVER' in the safety carve-out "
        "to make clear that safety/security work is exempt from the necessity gate."
    )


def test_assembled_developer_prompt_no_unrendered_placeholders() -> None:
    """The assembled developer prompt must not contain any unrendered ``{{`` placeholders.

    Guards against a future developer.md template variable going unsubstituted
    and leaking raw Jinja/mustache syntax into the coder's system prompt.
    """
    prompt = _assembled_developer_prompt()
    assert "{{" not in prompt, (
        "Assembled developer prompt contains unrendered '{{' placeholder(s). "
        "Ensure all template variables in developer.md are substituted before "
        "the prompt is passed to the coder agent."
    )
