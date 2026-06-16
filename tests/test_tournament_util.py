"""Unit tests for the hoisted :mod:`tournament.util` text helpers (v0.42.1 F2).

``_limit`` previously lived as byte-identical copies in ``phase_review.py`` and
``impl_tournament.py``. v0.42.1 hoists it to a single shared module so the plan
tournament can reuse the same proven truncation (the A4 unbounded-input fix).
"""

from tournament.util import _limit


def test_limit_returns_short_text_unchanged() -> None:
    assert _limit("short", 100) == "short"


def test_limit_returns_text_at_exact_boundary_unchanged() -> None:
    assert _limit("exact", 5) == "exact"


def test_limit_truncates_and_marks_dropped_bytes() -> None:
    text = "a" * 200
    out = _limit(text, 50)
    assert out.startswith("a" * 50)
    assert out == "a" * 50 + "\n... (truncated 150 bytes)"
    # Truncated output is materially shorter than the input.
    assert len(out) < len(text) + 40


def test_limit_handles_none_as_empty_string() -> None:
    assert _limit(None, 50) == ""  # type: ignore[arg-type]
