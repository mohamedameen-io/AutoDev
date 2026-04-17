"""Tests for the character-bigram Jaccard similarity helper."""

from __future__ import annotations

import pytest

from state.knowledge import jaccard_bigrams


def test_identical_strings_are_one() -> None:
    assert jaccard_bigrams("hello world", "hello world") == pytest.approx(1.0)


def test_identical_short_strings_are_one() -> None:
    # "ab" -> {(a,b)}, identical -> 1.0
    assert jaccard_bigrams("ab", "ab") == pytest.approx(1.0)


def test_disjoint_strings_are_zero() -> None:
    # Character bigrams share nothing when alphabets are disjoint.
    assert jaccard_bigrams("abcdef", "ghijkl") == pytest.approx(0.0)


def test_partial_overlap_between_zero_and_one() -> None:
    a = "use the existing filelock wrapper"
    b = "prefer the filelock wrapper over raw locks"
    sim = jaccard_bigrams(a, b)
    assert 0.0 < sim < 1.0


def test_empty_strings_return_zero() -> None:
    """Both empty strings -> no bigrams -> 0.0 (treat as incomparable)."""
    assert jaccard_bigrams("", "") == pytest.approx(0.0)


def test_single_empty_returns_zero() -> None:
    assert jaccard_bigrams("abc", "") == pytest.approx(0.0)
    assert jaccard_bigrams("", "abc") == pytest.approx(0.0)


def test_single_character_returns_zero() -> None:
    # Length-1 strings have no bigrams.
    assert jaccard_bigrams("a", "a") == pytest.approx(0.0)


def test_case_insensitive() -> None:
    assert jaccard_bigrams("Hello", "HELLO") == pytest.approx(
        jaccard_bigrams("hello", "hello")
    )


def test_symmetric() -> None:
    a = "always pin dependencies in lockfiles"
    b = "pin deps in lockfiles for reproducibility"
    assert jaccard_bigrams(a, b) == pytest.approx(jaccard_bigrams(b, a))


def test_threshold_range() -> None:
    """A highly similar pair clears the default 0.6 threshold."""
    a = "always prefer async file locks over polling"
    b = "prefer async file locks over polling loops"
    sim = jaccard_bigrams(a, b)
    assert sim >= 0.6, f"expected >=0.6 but got {sim:.3f}"
