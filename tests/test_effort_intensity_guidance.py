"""Tests for B3: effort-modulated minimalism intensity in tournament prompts.

``effort_intensity_guidance(user_complexity)`` maps effort level to an
intensity guidance string:

    - low    → "lite": minimal-change bias, suppresses speculative
                optimization / refactoring / scope expansion
    - medium → standard (no extra instruction — baseline necessity ladder)
    - high   → "deeper-work": refactors/optimizations in touched code in scope
    - max    → same as high (deeper-work allowed)

NON-NEGOTIABLE: the safety/validation/security carve-out from B1 must be
present at ALL effort levels, including low/"lite". Low effort is never an
excuse to drop validation/security.

``build_developer_prompt(base_prompt, user_complexity=None)`` is the
single injection point for both the necessity ladder AND the effort-modulated
intensity. These tests verify the assembled output string, not just the
constants, so they stay in sync with every production injection site.
"""

from __future__ import annotations

import pytest

from tournament.effort import EFFORT_INTENSITY, effort_intensity_guidance
from tournament.prompts import build_developer_prompt


# ---------------------------------------------------------------------------
# effort_intensity_guidance: unit tests on the mapping function
# ---------------------------------------------------------------------------


def test_low_effort_returns_lite_intensity() -> None:
    """Low effort maps to 'lite' — minimal-change bias."""
    text = effort_intensity_guidance("low")
    assert text, "low effort must return non-empty guidance"


def test_medium_effort_returns_empty_intensity() -> None:
    """Medium effort returns empty string (necessity-ladder-only baseline, no intensity text)."""
    text = effort_intensity_guidance("medium")
    # medium is the standard baseline — no extra modulation text added
    assert text == "", (
        "medium effort must return empty string; the necessity ladder itself is the baseline"
    )


def test_high_effort_returns_deeper_work_intensity() -> None:
    """High effort maps to 'deeper-work' — refactors/optimizations allowed."""
    text = effort_intensity_guidance("high")
    assert text, "high effort must return non-empty guidance"


def test_max_effort_returns_deeper_work_intensity() -> None:
    """Max effort maps to the same deeper-work guidance as high."""
    text = effort_intensity_guidance("max")
    assert text, "max effort must return non-empty guidance"


def test_high_and_max_intensity_are_same() -> None:
    """High and max should produce the same intensity guidance."""
    assert effort_intensity_guidance("high") == effort_intensity_guidance("max"), (
        "high and max effort must map to the same deeper-work intensity text"
    )


# ---------------------------------------------------------------------------
# Content requirements: low/"lite" effort guidance
# ---------------------------------------------------------------------------


def test_low_effort_guidance_suppresses_speculative_optimization() -> None:
    """Low effort guidance must explicitly suppress speculative optimization."""
    text = effort_intensity_guidance("low").lower()
    has_suppress = (
        "speculative" in text
        or "optimization" in text
        or "minimal" in text
        or "minimum" in text
    )
    assert has_suppress, (
        "Low effort guidance must bias toward the minimal change and "
        "suppress speculative optimization (found: {!r})".format(text[:200])
    )


def test_low_effort_guidance_suppresses_speculative_refactoring() -> None:
    """Low effort guidance must suppress speculative refactoring."""
    text = effort_intensity_guidance("low").lower()
    has_suppress = (
        "refactor" in text
        or "speculative" in text
        or "scope expansion" in text
        or "minimal" in text
    )
    assert has_suppress, (
        "Low effort guidance must suppress speculative refactoring "
        "and scope expansion beyond what the task requires."
    )


def test_high_effort_guidance_allows_deeper_work() -> None:
    """High effort guidance must permit deeper refactors/optimizations."""
    text = effort_intensity_guidance("high").lower()
    has_deeper = (
        "refactor" in text
        or "deeper" in text
        or "optimization" in text
        or "improve" in text
    )
    assert has_deeper, (
        "High effort guidance must state that refactors/optimizations that "
        "genuinely improve touched code are in scope."
    )


# ---------------------------------------------------------------------------
# EFFORT_INTENSITY table completeness
# ---------------------------------------------------------------------------


def test_effort_intensity_table_covers_all_levels() -> None:
    """EFFORT_INTENSITY must cover all four effort levels."""
    assert set(EFFORT_INTENSITY) == {"low", "medium", "high", "max"}, (
        "EFFORT_INTENSITY table must have keys low/medium/high/max"
    )


def test_effort_intensity_guidance_matches_table() -> None:
    """effort_intensity_guidance must be consistent with EFFORT_INTENSITY."""
    for level in ("low", "medium", "high", "max"):
        assert effort_intensity_guidance(level) == EFFORT_INTENSITY[level]


# ---------------------------------------------------------------------------
# build_developer_prompt: effort-modulated assembled output
# ---------------------------------------------------------------------------


def _dev_prompt(user_complexity: str | None = None) -> str:
    """Helper — assemble a developer prompt with the given effort level."""
    base = "You are a developer. Do the task."
    return build_developer_prompt(base, user_complexity=user_complexity)


def test_build_developer_prompt_low_effort_contains_minimal_bias() -> None:
    """Assembled low-effort developer prompt must contain minimal-change bias."""
    text = _dev_prompt("low").lower()
    has_minimal = (
        "minimal" in text
        or "minimum" in text
        or "speculative" in text
    )
    assert has_minimal, (
        "build_developer_prompt(base, user_complexity='low') must inject "
        "minimal-change bias text."
    )


def test_build_developer_prompt_high_effort_contains_deeper_work() -> None:
    """Assembled high-effort developer prompt must permit deeper work."""
    text = _dev_prompt("high").lower()
    has_deeper = (
        "deeper" in text
        or "refactor" in text
        or "optimization" in text
        or "improve" in text
    )
    assert has_deeper, (
        "build_developer_prompt(base, user_complexity='high') must inject "
        "deeper-work-allowed text."
    )


def test_build_developer_prompt_max_effort_contains_deeper_work() -> None:
    """Assembled max-effort developer prompt must permit deeper work."""
    text = _dev_prompt("max").lower()
    has_deeper = (
        "deeper" in text
        or "refactor" in text
        or "optimization" in text
        or "improve" in text
    )
    assert has_deeper, (
        "build_developer_prompt(base, user_complexity='max') must inject "
        "deeper-work-allowed text."
    )


# ---------------------------------------------------------------------------
# NON-NEGOTIABLE: safety carve-out present at EVERY effort level
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("effort", ["low", "medium", "high", "max"])
def test_safety_carve_out_present_at_all_effort_levels(effort: str) -> None:
    """Safety/validation/security carve-out must appear at EVERY effort level.

    This pins the B1 non-negotiable: low 'lite' effort is NEVER an excuse
    to drop input validation, error handling, or security work. The safety
    carve-out must survive regardless of effort.
    """
    text = _dev_prompt(effort).lower()
    has_safety = "safety" in text or "security" in text
    assert has_safety, (
        f"build_developer_prompt(base, user_complexity={effort!r}) must contain the "
        "safety/security carve-out. Low/lite effort must never suppress safety, "
        "input validation, error handling, or security work."
    )


@pytest.mark.parametrize("effort", ["low", "medium", "high", "max"])
def test_never_skipped_clause_present_at_all_effort_levels(effort: str) -> None:
    """The 'NEVER skipped' carve-out clause must be present at every effort level."""
    text = _dev_prompt(effort).lower()
    assert "never" in text, (
        f"build_developer_prompt(base, user_complexity={effort!r}) must contain "
        "'NEVER' in the safety carve-out (low effort is not an excuse to drop "
        "validation/security)."
    )


@pytest.mark.parametrize("effort", ["low", "medium", "high", "max"])
def test_necessity_ladder_present_at_all_effort_levels(effort: str) -> None:
    """The necessity ladder rungs must be present at every effort level."""
    text = _dev_prompt(effort).lower()
    assert "standard library" in text, (
        f"Effort {effort!r}: necessity ladder 'standard library' rung missing."
    )
    assert "existing" in text, (
        f"Effort {effort!r}: necessity ladder 'existing' (project dep/code) rung missing."
    )


# ---------------------------------------------------------------------------
# Backward compatibility: None user_complexity → standard (medium) behaviour
# ---------------------------------------------------------------------------


def test_build_developer_prompt_none_complexity_backward_compat() -> None:
    """Calling build_developer_prompt without user_complexity is backward-compatible.

    None should produce the same result as 'medium' (standard baseline).
    """
    base = "You are a developer."
    prompt_none = build_developer_prompt(base, user_complexity=None)
    prompt_medium = build_developer_prompt(base, user_complexity="medium")
    assert prompt_none == prompt_medium, (
        "build_developer_prompt(base) (no user_complexity) must equal "
        "build_developer_prompt(base, user_complexity='medium') for backward "
        "compatibility with call sites that haven't been updated yet."
    )


def test_build_developer_prompt_default_still_has_necessity_ladder() -> None:
    """The backward-compat default (None) must still include the B1 necessity ladder."""
    text = _dev_prompt(None).lower()
    assert "standard library" in text
    assert "safety" in text or "security" in text
    assert "never" in text


# ---------------------------------------------------------------------------
# Architect-B system: effort-modulated via ARCHITECT_B_SYSTEM or intensity
# (architect always gets high/max reasoning so no lite suppression needed,
#  but the safety carve-out must still be present)
# ---------------------------------------------------------------------------


def test_architect_b_system_safety_carve_out_unchanged() -> None:
    """ARCHITECT_B_SYSTEM must still contain the safety/security carve-out.

    B3 modulates developer prompt intensity; it must NOT weaken the architect
    guidance. Regression test to confirm B3 implementation doesn't remove
    the B1 carve-out from ARCHITECT_B_SYSTEM.
    """
    from tournament.prompts import ARCHITECT_B_SYSTEM

    text = ARCHITECT_B_SYSTEM.lower()
    assert "safety" in text or "security" in text, (
        "ARCHITECT_B_SYSTEM must still contain the safety/security carve-out "
        "after B3 implementation."
    )
    assert "never" in text, (
        "ARCHITECT_B_SYSTEM must still contain 'never' in the carve-out "
        "after B3 implementation."
    )
