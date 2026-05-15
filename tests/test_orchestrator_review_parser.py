"""Tests for the hardened reviewer-verdict parser.

v0.31.0 (Phase 1.3) hardened :func:`orchestrator.execute_phase._parse_review_verdict`:

* Empty / whitespace-only input → ``("MALFORMED", ["empty reviewer response"])``
  (was ``("NEEDS_CHANGES", [...])``; the legacy issues string is preserved
  for backward-compat with monitoring).
* Prose with no verdict keyword → ``MALFORMED`` (was a silent ``APPROVED``,
  the most dangerous possible default — Hypothesis B in the recovery plan).
* Verdict keyword can appear ANYWHERE in the response, not just on the
  first non-empty line.
* Strict ``VERDICT: <KEYWORD>`` line takes precedence over any later prose
  match.
* All three verdicts (APPROVED, NEEDS_CHANGES, REJECTED) round-trip
  through the parser.
"""

from __future__ import annotations

from orchestrator.execute_phase import _parse_review_verdict


def test_parse_review_verdict_empty_input() -> None:
    """Empty string → MALFORMED with the legacy issues string preserved."""
    verdict, issues = _parse_review_verdict("")
    assert verdict == "MALFORMED"
    assert issues == ["empty reviewer response"]


def test_parse_review_verdict_whitespace_only_input() -> None:
    """Whitespace-only input is indistinguishable from empty for the parser."""
    verdict, issues = _parse_review_verdict("   \n\t\n  ")
    assert verdict == "MALFORMED"
    assert issues == ["empty reviewer response"]


def test_parse_review_verdict_does_not_silently_default_to_approved() -> None:
    """v0.31.0 (Phase 1.3): prose with no verdict keyword → MALFORMED.

    The pre-v0.31.0 parser would silently return APPROVED here, which is
    the unsafest possible default for a code-review machinery failure.
    """
    text = (
        "I looked at the diff. It seems fine I guess. "
        "There are some things I would do differently but nothing critical."
    )
    verdict, issues = _parse_review_verdict(text)
    assert verdict == "MALFORMED"
    assert verdict != "APPROVED"


def test_parse_review_verdict_finds_verdict_anywhere() -> None:
    """A verdict on line 5 of a multi-line response is parsed correctly."""
    text = (
        "Reviewing the diff now.\n"
        "First impression: the change touches three files.\n"
        "Looking at the test coverage.\n"
        "All assertions look correct.\n"
        "VERDICT: APPROVED\n"
    )
    verdict, _issues = _parse_review_verdict(text)
    assert verdict == "APPROVED"


def test_parse_review_verdict_recognises_three_verdicts() -> None:
    """APPROVED, NEEDS_CHANGES, REJECTED all parse correctly."""
    for token in ("APPROVED", "NEEDS_CHANGES", "REJECTED"):
        verdict, _ = _parse_review_verdict(f"VERDICT: {token}\n")
        assert verdict == token, f"failed to parse {token!r}"


def test_parse_review_verdict_strict_form_with_list_marker() -> None:
    """Reviewers occasionally prefix the verdict line with ``- `` — accept it."""
    verdict, _ = _parse_review_verdict("- VERDICT: NEEDS_CHANGES\n- issue 1")
    assert verdict == "NEEDS_CHANGES"


def test_parse_review_verdict_recognises_needs_changes_with_space() -> None:
    """Some reviewers emit ``NEEDS CHANGES`` (with a space) — normalise."""
    text = "Looking now.\nVERDICT: NEEDS CHANGES\n"
    verdict, _ = _parse_review_verdict(text)
    assert verdict == "NEEDS_CHANGES"


def test_parse_review_verdict_legacy_first_line_keyword_still_works() -> None:
    """Reviewers that pre-date the strict prompt still work via fallback."""
    text = "APPROVED\n- minor: docstring spacing\n"
    verdict, issues = _parse_review_verdict(text)
    assert verdict == "APPROVED"
    assert issues == ["minor: docstring spacing"]


def test_parse_review_verdict_extracts_bullet_issues() -> None:
    """Bullet-marker lines are extracted into the issues list."""
    text = (
        "VERDICT: NEEDS_CHANGES\n"
        "- error handling missing\n"
        "* tests do not cover the new branch\n"
        "  some prose here that is not a bullet\n"
        "- third issue\n"
    )
    verdict, issues = _parse_review_verdict(text)
    assert verdict == "NEEDS_CHANGES"
    assert issues == [
        "error handling missing",
        "tests do not cover the new branch",
        "third issue",
    ]


def test_parse_review_verdict_strict_wins_over_later_prose_match() -> None:
    """A strict ``VERDICT:`` line takes precedence over a later prose mention.

    Reviewers occasionally restate alternatives ("could be APPROVED if X")
    after their actual verdict — the strict line is the canonical signal.
    """
    text = (
        "VERDICT: REJECTED\n"
        "- security regression\n"
        "Note: this could be APPROVED once the input validation lands.\n"
    )
    verdict, _ = _parse_review_verdict(text)
    assert verdict == "REJECTED"


def test_parse_review_verdict_lowercase_strict_form() -> None:
    """The strict ``VERDICT:`` regex is case-insensitive on the keyword."""
    text = "verdict: approved\n"
    verdict, _ = _parse_review_verdict(text)
    assert verdict == "APPROVED"
