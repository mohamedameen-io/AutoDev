"""Tests for v0.31.0 (Phase 5.4) adapter fitness scoring."""

from __future__ import annotations

from adapters.fitness import (
    WARNING_THRESHOLD,
    compute_fitness_score,
    get_fitness_warning,
)


def test_cursor_high_ts_js() -> None:
    """Cursor scores 95 when TS+JS share crosses 50%."""
    profile = {"typescript": 0.60, "python": 0.40}
    assert compute_fitness_score("cursor", profile) == 95.0
    assert get_fitness_warning("cursor", profile) is None

    profile_split = {"typescript": 0.30, "javascript": 0.25, "python": 0.45}
    assert compute_fitness_score("cursor", profile_split) == 95.0


def test_cursor_low_ts_js() -> None:
    """Cursor scores 30 (warns) when TS+JS share is < 10%."""
    profile = {"python": 1.0}
    score = compute_fitness_score("cursor", profile)
    assert score == 30.0
    warn = get_fitness_warning("cursor", profile)
    assert warn is not None
    assert "cursor" in warn.lower()
    assert "30" in warn

    # 30% TS+JS lands in the 80 bucket (no warning).
    profile_30 = {"typescript": 0.30, "python": 0.70}
    assert compute_fitness_score("cursor", profile_30) == 80.0
    assert get_fitness_warning("cursor", profile_30) is None

    # 10% TS+JS lands in the 60 bucket (no warning -- 60 >= 50).
    profile_10 = {"typescript": 0.10, "python": 0.90}
    assert compute_fitness_score("cursor", profile_10) == 60.0
    assert get_fitness_warning("cursor", profile_10) is None


def test_claude_language_agnostic() -> None:
    """Claude scores 85 baseline regardless of language; +5 for python>=40%."""
    py_heavy = {"python": 0.80, "go": 0.20}
    assert compute_fitness_score("claude_code", py_heavy) == 90.0

    ts_heavy = {"typescript": 0.90, "python": 0.10}
    assert compute_fitness_score("claude_code", ts_heavy) == 85.0

    mixed = {"go": 0.40, "rust": 0.30, "java": 0.30}
    assert compute_fitness_score("claude_code", mixed) == 85.0

    # No warning on baseline.
    assert get_fitness_warning("claude_code", ts_heavy) is None


def test_unknown_adapter() -> None:
    """Unknown adapter scores baseline 50 -- no warning, no enthusiasm."""
    profile = {"python": 1.0}
    assert compute_fitness_score("windsurf", profile) == 50.0
    # 50 >= WARNING_THRESHOLD (50) so no warning.
    assert get_fitness_warning("windsurf", profile) is None
    assert WARNING_THRESHOLD == 50.0
