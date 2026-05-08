"""v0.20.0 B1: per-event-type decay-curve tests."""

from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone

import pytest

from config.schema import DecayCurveConfig
from state.knowledge import KnowledgeEntry, _recency_factor


def _ts_age_days(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def test_recency_factor_legacy_path_unchanged_when_curve_none() -> None:
    """When ``curve=None``, behavior is byte-identical to pre-v0.20.0."""
    now = time.time()
    # 0d old -> 1.0
    fresh = (datetime.now(timezone.utc)).isoformat()
    assert math.isclose(_recency_factor(fresh, now), 1.0, abs_tol=1e-9)
    # 30d old -> 0.5
    thirty = _ts_age_days(30.0)
    assert math.isclose(_recency_factor(thirty, now), 0.5, abs_tol=1e-3)
    # 100d old -> floor 0.5
    hundred = _ts_age_days(100.0)
    assert _recency_factor(hundred, now) == 0.5
    # bad timestamp -> 0.5
    assert _recency_factor("not-a-date", now) == 0.5


def test_recency_factor_default_decay_curve_byte_identical_to_legacy() -> None:
    """``DecayCurveConfig()`` defaults match the legacy 30-day linear curve."""
    now = time.time()
    curve = DecayCurveConfig()  # defaults: half_life_days=15, floor=0.5
    for age in (0.0, 5.0, 15.0, 25.0, 30.0, 60.0):
        ts = _ts_age_days(age)
        legacy = _recency_factor(ts, now, curve=None)
        new_path = _recency_factor(ts, now, curve=curve)
        assert math.isclose(legacy, new_path, abs_tol=1e-3), (
            f"age={age}: legacy={legacy} new={new_path}"
        )


def test_recency_factor_slow_decay_curve() -> None:
    """A curve with half_life_days=60 decays slower than legacy."""
    now = time.time()
    slow = DecayCurveConfig(half_life_days=60.0, floor=0.5)
    # 30d old: legacy = 0.5; slow curve still well above floor.
    ts = _ts_age_days(30.0)
    legacy = _recency_factor(ts, now, curve=None)
    slow_factor = _recency_factor(ts, now, curve=slow)
    assert slow_factor > legacy + 0.1


def test_recency_factor_fast_decay_curve() -> None:
    """A curve with half_life_days=3 decays faster than legacy."""
    now = time.time()
    fast = DecayCurveConfig(half_life_days=3.0, floor=0.5)
    # 30d old: legacy at floor; fast curve at floor.
    # 5d old: legacy still high; fast curve closer to floor.
    ts = _ts_age_days(5.0)
    legacy = _recency_factor(ts, now, curve=None)
    fast_factor = _recency_factor(ts, now, curve=fast)
    assert fast_factor < legacy - 0.05


def test_recency_factor_floor_clamps_old_entries() -> None:
    """Entries beyond ``2 * half_life_days`` clamp to floor."""
    now = time.time()
    curve = DecayCurveConfig(half_life_days=10.0, floor=0.3)
    very_old = _ts_age_days(50.0)
    assert math.isclose(_recency_factor(very_old, now, curve=curve), 0.3, abs_tol=1e-9)


def test_recency_factor_zero_age_returns_one() -> None:
    """A zero-age entry uses the full 1.0 weight even with a custom curve."""
    now = time.time()
    fresh = datetime.now(timezone.utc).isoformat()
    curve = DecayCurveConfig(half_life_days=10.0, floor=0.2)
    f = _recency_factor(fresh, now, curve=curve)
    assert math.isclose(f, 1.0, abs_tol=1e-3)


def test_decay_curve_config_validation_rejects_negative_half_life() -> None:
    with pytest.raises(Exception):
        DecayCurveConfig(half_life_days=-1.0)


def test_decay_curve_config_validation_rejects_floor_above_one() -> None:
    with pytest.raises(Exception):
        DecayCurveConfig(half_life_days=10.0, floor=1.5)


def test_recency_factor_bad_timestamp_returns_floor_when_curve_set() -> None:
    """A bad timestamp produces the curve's floor (not the legacy 0.5)."""
    now = time.time()
    curve = DecayCurveConfig(half_life_days=10.0, floor=0.2)
    assert _recency_factor("not-a-date", now, curve=curve) == 0.2


def test_knowledge_entry_metadata_event_type_round_trip() -> None:
    """Sanity: KnowledgeEntry.metadata accepts ``event_type`` for v0.20.0."""
    e = KnowledgeEntry(
        id="t-1",
        timestamp=_ts_age_days(0.0),
        role_source="critic_t",
        tier="swarm",
        text="hello",
        metadata={"event_type": "winner_promoted", "family": "plan-tournament"},
    )
    assert e.metadata.get("event_type") == "winner_promoted"
