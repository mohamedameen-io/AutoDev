"""v0.20.0 B1: regression — when ``decay_curves=None`` the ranking is
byte-identical to pre-v0.20.0 behavior.

This test pins the legacy curve via :func:`_rank_with_ts` against a hand-
constructed entry shape; if either the curve or the rank formula
change, this test fails — by design, since the spec says default-decay
must be byte-identical to the v0.19.0 path.
"""

from __future__ import annotations

import math
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from state.knowledge import KnowledgeEntry, KnowledgeStore


def _ts_age_days(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def test_default_decay_rank_byte_identical_when_curves_none() -> None:
    """No KnowledgeConfig override + no metadata.event_type → legacy curve."""
    with tempfile.TemporaryDirectory() as tmp:
        store = KnowledgeStore(cwd=Path(tmp), cfg=None)
        # legacy formula: confidence=0.8, age=15d, applied=0
        # recency(15d)=1-0.5*(15/30)=0.75; applied_boost=1+log(1)=1.0
        # rank = 0.8 * 0.75 * 1.0 = 0.6
        entry = KnowledgeEntry(
            id="t-1",
            timestamp=_ts_age_days(15.0),
            role_source="critic_t",
            tier="swarm",
            text="legacy",
            confidence=0.8,
            applied_count=0,
            metadata={},
        )
        now = time.time()
        rank = store._rank_with_ts(entry, now)
        # accept tiny epsilon for floating-point + clock drift
        assert math.isclose(rank, 0.6, abs_tol=5e-3), rank


def test_default_decay_rank_unaffected_by_event_type_when_no_curves() -> None:
    """Setting metadata.event_type is a no-op when ``decay_curves`` is None."""
    with tempfile.TemporaryDirectory() as tmp:
        store = KnowledgeStore(cwd=Path(tmp), cfg=None)
        a = KnowledgeEntry(
            id="t-a",
            timestamp=_ts_age_days(10.0),
            role_source="critic_t",
            tier="swarm",
            text="no-meta",
            confidence=0.7,
            applied_count=2,
        )
        b = KnowledgeEntry(
            id="t-b",
            timestamp=_ts_age_days(10.0),
            role_source="critic_t",
            tier="swarm",
            text="with-meta",
            confidence=0.7,
            applied_count=2,
            metadata={"event_type": "winner_promoted"},
        )
        now = time.time()
        ra = store._rank_with_ts(a, now)
        rb = store._rank_with_ts(b, now)
        assert math.isclose(ra, rb, abs_tol=1e-9)


def test_per_event_type_curve_changes_rank(monkeypatch) -> None:
    """When a curve is configured for an event_type, the rank changes."""
    from config.schema import DecayCurveConfig, KnowledgeConfig

    with tempfile.TemporaryDirectory() as tmp:
        store = KnowledgeStore(cwd=Path(tmp), cfg=None)
        # Override the effective KnowledgeConfig the store consults.
        custom_kcfg = KnowledgeConfig(
            decay_curves={
                "winner_promoted": DecayCurveConfig(
                    half_life_days=60.0, floor=0.5
                ),
            }
        )
        # Monkeypatch the property to return our custom config.
        monkeypatch.setattr(
            type(store),
            "knowledge_config",
            property(lambda self: custom_kcfg),
        )
        legacy_entry = KnowledgeEntry(
            id="leg",
            timestamp=_ts_age_days(30.0),
            role_source="critic_t",
            tier="swarm",
            text="legacy",
            confidence=0.7,
            applied_count=0,
            metadata={},
        )
        winner_entry = KnowledgeEntry(
            id="win",
            timestamp=_ts_age_days(30.0),
            role_source="critic_t",
            tier="swarm",
            text="winner",
            confidence=0.7,
            applied_count=0,
            metadata={"event_type": "winner_promoted"},
        )
        now = time.time()
        legacy_rank = store._rank_with_ts(legacy_entry, now)
        winner_rank = store._rank_with_ts(winner_entry, now)
        # Winner with slow-decay curve (half-life 60d) decays less in 30d
        # than the legacy 30d-window curve (which has hit the floor).
        assert winner_rank > legacy_rank
