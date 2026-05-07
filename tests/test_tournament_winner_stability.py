"""Unit tests for the ``_winner_window_stable`` detector and the
``winner_stability_window`` knob threaded through ``TournamentConfig``.

The detector is the orthogonal "non-A winner stable for K passes" runaway
case. It complements ``convergence_k`` (which already handles the A-streak
case) by halting when the synthesizer keeps producing AB/B candidates that
judges prefer pass after pass without genuine new content.

These tests exercise the helper directly with synthetic ``PassResult``
histories. End-to-end ``Tournament.run`` integration is in
``tests/test_tournament_runaway_repro.py``.
"""

from __future__ import annotations

from typing import Any

from tournament.core import PassResult, _winner_window_stable


def _make_pass(pass_num: int, effective_winner: str, raw_winner: str = "AB") -> PassResult:
    """Build a synthetic pass result with the given ``effective_winner``."""
    meta: dict[str, Any] = {"effective_winner": effective_winner}
    return PassResult(
        pass_num=pass_num,
        winner=raw_winner,  # type: ignore[arg-type]
        scores={"A": 0, "B": 0, "AB": 0},
        valid_judges=3,
        elapsed_s=0.1,
        incumbent_hash_before="hash_before",
        incumbent_hash_after="hash_after",
        meta=meta,
    )


def test_winner_stability_fires_on_3_identical_AB_winners() -> None:
    """3 trailing passes all with effective_winner=AB → stable."""
    history = [
        _make_pass(1, "AB"),
        _make_pass(2, "AB"),
        _make_pass(3, "AB"),
    ]
    assert _winner_window_stable(history, window=3) is True


def test_winner_stability_fires_on_3_identical_B_winners() -> None:
    """3 trailing passes all with effective_winner=B → stable."""
    history = [
        _make_pass(1, "B"),
        _make_pass(2, "B"),
        _make_pass(3, "B"),
    ]
    assert _winner_window_stable(history, window=3) is True


def test_winner_stability_does_not_fire_on_mixed_winners() -> None:
    """Trailing window contains different winners → unstable."""
    history = [
        _make_pass(1, "AB"),
        _make_pass(2, "B"),
        _make_pass(3, "AB"),
    ]
    assert _winner_window_stable(history, window=3) is False


def test_winner_stability_does_not_fire_on_A_streak() -> None:
    """A-streak is owned by ``convergence_k``; ``_winner_window_stable`` must NOT fire.

    This guarantees the two detectors don't double-count the same situation.
    """
    history = [
        _make_pass(1, "A"),
        _make_pass(2, "A"),
        _make_pass(3, "A"),
    ]
    assert _winner_window_stable(history, window=3) is False


def test_winner_stability_does_not_fire_with_short_history() -> None:
    """Window=3 with len(history)=2 → not enough data → False."""
    history = [
        _make_pass(1, "AB"),
        _make_pass(2, "AB"),
    ]
    assert _winner_window_stable(history, window=3) is False


def test_winner_stability_uses_only_trailing_window() -> None:
    """Earlier passes outside the trailing window must not affect the result."""
    history = [
        _make_pass(1, "B"),
        _make_pass(2, "A"),
        _make_pass(3, "AB"),
        _make_pass(4, "AB"),
        _make_pass(5, "AB"),
    ]
    # Trailing 3 passes are all AB → stable, despite earlier mix.
    assert _winner_window_stable(history, window=3) is True


def test_winner_stability_window_2_fires_for_impl() -> None:
    """Impl tournament uses window=2 (max_rounds=3 makes 3 unsafe)."""
    history = [
        _make_pass(1, "AB"),
        _make_pass(2, "AB"),
    ]
    assert _winner_window_stable(history, window=2) is True


def test_winner_stability_window_2_does_not_fire_on_A() -> None:
    """Even at window=2 the A-only branch must not trip."""
    history = [
        _make_pass(1, "A"),
        _make_pass(2, "A"),
    ]
    assert _winner_window_stable(history, window=2) is False
