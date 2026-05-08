"""v0.20.0 A2: regression-based plateau detector tests."""

from __future__ import annotations

import math
import time

import pytest

from orchestrator.plateau_detector import PlateauDetector, _ols_slope
from state.knowledge import KnowledgeEntry


def test_ols_slope_strictly_increasing() -> None:
    """A line with slope 1 returns slope 1.0."""
    assert math.isclose(_ols_slope([0, 1, 2, 3, 4]), 1.0, abs_tol=1e-9)


def test_ols_slope_flat_returns_zero() -> None:
    """A flat sequence returns slope 0.0."""
    assert math.isclose(_ols_slope([3, 3, 3, 3]), 0.0, abs_tol=1e-9)


def test_ols_slope_partial_progress() -> None:
    """A sequence with one win in 10 events returns slope ~0.05-0.1 (small)."""
    seq = [0, 0, 0, 0, 0, 1, 1, 1, 1, 1]
    s = _ols_slope(seq)
    assert 0.05 < s < 0.20


def test_ols_slope_degenerate_inputs() -> None:
    assert _ols_slope([]) == 0.0
    assert _ols_slope([5]) == 0.0


def test_ols_slope_negative_trend() -> None:
    """Cumulative wins can never decrease in real data, but the helper
    handles arbitrary sequences. Sanity check on a negative slope input."""
    assert _ols_slope([5, 4, 3, 2, 1]) == -1.0


# ---------------------------------------------------------------------------
# detect_plateau_regression: integration-ish — uses a stubbed KnowledgeStore
# ---------------------------------------------------------------------------


class _StubKnowledge:
    def __init__(self, entries: list[KnowledgeEntry]) -> None:
        self._entries = entries

    async def read_all(self, tier: str = "swarm") -> list[KnowledgeEntry]:
        return list(self._entries)


def _entry(
    event_type: str,
    family: str,
    ts_offset: float = 0.0,
) -> KnowledgeEntry:
    return KnowledgeEntry(
        id=f"e-{ts_offset}",
        timestamp=f"2024-01-{int(1 + ts_offset):02d}T00:00:00+00:00",
        role_source="critic_t",
        tier="swarm",
        text=f"event {event_type}",
        confidence=0.5,
        applied_count=0,
        metadata={"event_type": event_type, "family": family},
    )


@pytest.mark.asyncio
async def test_detect_plateau_regression_low_slope_flags_plateau() -> None:
    """All-discard window → cumulative wins flat → slope 0 → plateau."""
    entries = [_entry("discard", "plan", i) for i in range(5)]
    pd = PlateauDetector(_StubKnowledge(entries))  # type: ignore[arg-type]
    is_plateau = await pd.detect_plateau_regression(
        "plan", window=5, slope_threshold=0.1
    )
    assert is_plateau is True


@pytest.mark.asyncio
async def test_detect_plateau_regression_steady_wins_no_plateau() -> None:
    """A win every event → slope 1 → not a plateau."""
    entries = [_entry("winner_promoted", "plan", i) for i in range(5)]
    pd = PlateauDetector(_StubKnowledge(entries))  # type: ignore[arg-type]
    is_plateau = await pd.detect_plateau_regression(
        "plan", window=5, slope_threshold=0.1
    )
    assert is_plateau is False


@pytest.mark.asyncio
async def test_detect_plateau_regression_late_winner_above_threshold() -> None:
    """A single win late in the window: slope ~0.04 → plateau (slope < 0.1)."""
    entries = (
        [_entry("discard", "plan", i) for i in range(8)]
        + [_entry("winner_promoted", "plan", 9)]
    )
    pd = PlateauDetector(_StubKnowledge(entries))  # type: ignore[arg-type]
    # default slope_threshold=0.1 — single late win has slope ~0.04 < 0.1
    is_plateau = await pd.detect_plateau_regression(
        "plan", window=10, slope_threshold=0.1
    )
    assert is_plateau is True


@pytest.mark.asyncio
async def test_detect_plateau_regression_cold_start_returns_false() -> None:
    """<3 events: not enough data; return False."""
    entries = [_entry("discard", "plan", 0)]
    pd = PlateauDetector(_StubKnowledge(entries))  # type: ignore[arg-type]
    is_plateau = await pd.detect_plateau_regression(
        "plan", window=10, slope_threshold=0.1
    )
    assert is_plateau is False


@pytest.mark.asyncio
async def test_detect_plateau_regression_filters_by_family() -> None:
    """Entries from other families are ignored."""
    entries = (
        [_entry("winner_promoted", "other", i) for i in range(5)]
        + [_entry("discard", "plan", 5 + i) for i in range(5)]
    )
    pd = PlateauDetector(_StubKnowledge(entries))  # type: ignore[arg-type]
    # Plan family is all-discard → plateau
    is_plateau = await pd.detect_plateau_regression(
        "plan", window=5, slope_threshold=0.1
    )
    assert is_plateau is True


@pytest.mark.asyncio
async def test_detect_cross_family_plateau_regression_no_winners() -> None:
    """Cross-family: all events are discards → flat slope → plateau."""
    entries = [_entry("discard", "fam-{}".format(i % 2), i) for i in range(6)]
    pd = PlateauDetector(_StubKnowledge(entries))  # type: ignore[arg-type]
    is_plateau = await pd.detect_cross_family_plateau_regression(
        window=10, slope_threshold=0.1
    )
    assert is_plateau is True


@pytest.mark.asyncio
async def test_detect_cross_family_plateau_regression_with_wins() -> None:
    """Cross-family with healthy winner cadence → not a plateau."""
    entries = []
    for i in range(10):
        et = "winner_promoted" if i % 2 == 0 else "discard"
        entries.append(_entry(et, "fam-{}".format(i % 2), i))
    pd = PlateauDetector(_StubKnowledge(entries))  # type: ignore[arg-type]
    is_plateau = await pd.detect_cross_family_plateau_regression(
        window=10, slope_threshold=0.3
    )
    assert is_plateau is False


def test_plateau_detector_config_defaults() -> None:
    from config.schema import PlateauDetectorConfig

    cfg = PlateauDetectorConfig()
    assert cfg.strategy == "rules"
    assert cfg.regression_window >= 3
    assert cfg.plateau_slope_threshold >= 0.0


def test_plateau_detector_config_validation_rejects_negative_slope() -> None:
    from config.schema import PlateauDetectorConfig

    with pytest.raises(Exception):
        PlateauDetectorConfig(plateau_slope_threshold=-0.1)


def test_plateau_detector_config_validation_rejects_small_window() -> None:
    from config.schema import PlateauDetectorConfig

    with pytest.raises(Exception):
        PlateauDetectorConfig(regression_window=1)
