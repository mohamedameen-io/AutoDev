"""Tests for src.tournament.core.parse_ranking (ported from autoreason)."""

from __future__ import annotations

import pytest

from tournament import parse_ranking


class TestParseRanking:
    def test_comma_separated(self) -> None:
        assert parse_ranking("RANKING: 1, 3, 2") == ["1", "3", "2"]

    def test_bracketed(self) -> None:
        assert parse_ranking("ranking: [1,3,2]") == ["1", "3", "2"]

    def test_space_separated(self) -> None:
        assert parse_ranking("RANKING: 1 3 2") == ["1", "3", "2"]

    def test_prose_with_ranking_line(self) -> None:
        text = "I think the best is 1.\nThen 3, then 2.\nRANKING: 1, 3, 2"
        assert parse_ranking(text) == ["1", "3", "2"]

    def test_takes_last_ranking_line(self) -> None:
        text = "RANKING: 2, 1, 3\nSome discussion.\nRANKING: 1, 3, 2"
        assert parse_ranking(text) == ["1", "3", "2"]

    def test_leading_bold_and_hash(self) -> None:
        assert parse_ranking("**RANKING: 1, 2, 3**") == ["1", "2", "3"]
        assert parse_ranking("### RANKING: 3, 1, 2") == ["3", "1", "2"]

    def test_missing_ranking_line(self) -> None:
        assert parse_ranking("No ranking here, just prose.") is None

    def test_malformed_ranking_gives_none(self) -> None:
        # Letters only — no digits match the valid_labels filter.
        assert parse_ranking("RANKING: A, B, C", "123") is None

    def test_single_digit_rejected(self) -> None:
        # Only 1 digit found in the ranking line → treated as parse failure.
        assert parse_ranking("RANKING: 1") is None

    def test_two_digits_rejected_with_three_labels(self) -> None:
        # Incomplete ranking (2 of 3 candidates) is rejected to prevent Borda bias.
        assert parse_ranking("RANKING: 1, 2") is None

    def test_custom_valid_labels_for_5way(self) -> None:
        # 5-way judge uses A..E; the function supports custom label sets.
        text = "RANKING: A, C, B, E, D"
        assert parse_ranking(text, "ABCDE") == ["A", "C", "B", "E", "D"]

    def test_empty_string(self) -> None:
        assert parse_ranking("") is None

    def test_ranking_case_insensitive(self) -> None:
        assert parse_ranking("Ranking: 1, 3, 2") == ["1", "3", "2"]
        assert parse_ranking("ranking: 1, 3, 2") == ["1", "3", "2"]

    def test_ignores_non_label_digits(self) -> None:
        # In "123" valid-labels mode, a "4" or "5" gets filtered out.
        # After filtering, only 2 valid labels remain (incomplete) → rejected.
        assert parse_ranking("RANKING: 1, 5, 2") is None

    def test_multiline_ranking_scan_is_reversed(self) -> None:
        # Two RANKING lines — reverse scan finds the last one that starts
        # with "RANKING:" (after stripping bold/hash).
        text = "RANKING: 3, 2, 1\n\nRANKING: 1, 2, 3"
        assert parse_ranking(text) == ["1", "2", "3"]

    def test_ranking_must_start_the_line(self) -> None:
        # The autoreason implementation requires the stripped line to START
        # with "RANKING:" — it won't match "FINAL RANKING: ..." or prose
        # that happens to contain the word.
        text = "FINAL RANKING: 1, 2, 3"
        assert parse_ranking(text) is None


@pytest.mark.parametrize(
    "line,expected",
    [
        ("RANKING: 1, 3, 2", ["1", "3", "2"]),
        ("ranking: [1,3,2]", ["1", "3", "2"]),
        ("RANKING:1,3,2", ["1", "3", "2"]),
        ("RANKING:   1   3   2", ["1", "3", "2"]),
    ],
)
def test_whitespace_and_punctuation_variants(line: str, expected: list[str]) -> None:
    assert parse_ranking(line) == expected
