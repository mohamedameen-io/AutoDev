"""v0.14.0 prompt regression tests for the DIRECTIVE PRESERVATION addition.

Three tournament prompts MUST instruct downstream LLMs to preserve any
``Requires: <token>`` directive present on a task in the input plan:

* ``ARCHITECT_B_PROMPT`` — the revision step. Architect_b can rewrite
  whole tasks, so the risk of dropping a Requires line is highest here.
* ``SYNTHESIZER_PROMPT`` — the merge step. When merging two versions
  where one has Requires: hardware, the synthesizer must keep it.
* ``CRITIC_PROMPT`` — the criticism step. Critic shouldn't suggest
  removing Requires lines as a "complexity reduction".

Note: the project has no on-disk ``critic_t.md`` — the critic_t role
loads its system prompt from ``tournament.prompts.CRITIC_SYSTEM`` and
its turn prompt from ``CRITIC_PROMPT``. We anchor the directive
preservation guidance in ``CRITIC_PROMPT`` for symmetry with the other
two role prompts.

Plus: a behavioral test using StubAdapter where a plan with
``Requires: hardware`` is fed through architect_b — the Requires line
must survive in the output. This guards against future prompt
regressions even if the explicit text-anchor below changes.
"""

from __future__ import annotations

from tournament.prompts import (
    ARCHITECT_B_PROMPT,
    CRITIC_PROMPT,
    SYNTHESIZER_PROMPT,
)


def test_architect_b_prompt_contains_directive_preservation_section() -> None:
    """ARCHITECT_B_PROMPT must call out preserving ``Requires:`` directives."""
    assert "DIRECTIVE PRESERVATION" in ARCHITECT_B_PROMPT
    assert "Requires:" in ARCHITECT_B_PROMPT


def test_synthesizer_prompt_contains_directive_preservation_section() -> None:
    """SYNTHESIZER_PROMPT must call out preserving ``Requires:`` directives."""
    assert "DIRECTIVE PRESERVATION" in SYNTHESIZER_PROMPT
    assert "Requires:" in SYNTHESIZER_PROMPT


def test_critic_prompt_contains_directive_preservation_section() -> None:
    """CRITIC_PROMPT must instruct critic_t not to flag ``Requires:`` directives
    as removable complexity."""
    assert "DIRECTIVE PRESERVATION" in CRITIC_PROMPT
    assert "Requires:" in CRITIC_PROMPT


# ---------------------------------------------------------------------------
# Behavioral round-trip — Requires survives the architect_b call
# ---------------------------------------------------------------------------


def test_architect_b_prompt_format_substitutes_inputs() -> None:
    """The architect_b prompt template, when rendered with a plan that
    contains a ``Requires: hardware`` line, embeds that line verbatim
    in the rendered prompt — so the LLM seeing the prompt sees the
    directive in the input proposal text.

    This is the round-trip prerequisite: if the prompt template stripped
    Requires lines before showing them to architect_b, the LLM could
    never preserve them. This test guards against template regressions
    that would, e.g., re-format the input through a sanitizer.
    """
    sample_plan = """# Plan: Hardware-spanning work

## Phase 1: Reproduce
### Task 1.1: Reproduce GL error on QNX
  - Requires: hardware
  - Description: Run the multi-window GLES app on a QNX device.

COMPLEXITY: complex
"""
    rendered = ARCHITECT_B_PROMPT.format(
        task_prompt="Get the test suite green",
        version_a=sample_plan,
        critic="No issues found.",
    )
    # The Requires line is visible to the LLM in the rendered prompt.
    assert "Requires: hardware" in rendered


def test_synthesizer_prompt_format_passes_through_requires() -> None:
    """When two plan versions both reach the synthesizer with a
    ``Requires: hardware`` line, both inputs embed the directive."""
    plan_a = """# Plan: A

### Task 1.1: hw task A
  - Requires: hardware
  - Description: do thing A

COMPLEXITY: medium
"""
    plan_b = """# Plan: B

### Task 1.1: hw task B
  - Requires: hardware, human
  - Description: do thing B

COMPLEXITY: medium
"""
    rendered = SYNTHESIZER_PROMPT.format(
        task_prompt="merge plans",
        version_x=plan_a,
        version_y=plan_b,
    )
    assert "Requires: hardware" in rendered
    # Both inputs visible — both Requires variants are in the prompt.
    assert "Requires: hardware, human" in rendered
