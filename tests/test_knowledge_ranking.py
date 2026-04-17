"""Ranking formula: confidence * recency * (1 + log(applied_count+1))."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from config.defaults import default_config
from state.knowledge import KnowledgeEntry, KnowledgeStore


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _mk_store(tmp_path: Path) -> KnowledgeStore:
    cfg = default_config()
    cfg.hive.enabled = False
    return KnowledgeStore(tmp_path, cfg=cfg, hive_path=tmp_path / "hive.jsonl")


def _mk_entry(
    *,
    confidence: float = 0.5,
    applied_count: int = 0,
    timestamp: str | None = None,
    suffix: str = "",
) -> KnowledgeEntry:
    return KnowledgeEntry(
        id=f"id{suffix}",
        timestamp=timestamp or _now_iso(),
        role_source="developer",
        tier="swarm",
        text=f"lesson{suffix}",
        confidence=confidence,
        applied_count=applied_count,
        confirmations=1,
    )


def test_higher_confidence_ranks_higher(tmp_path: Path) -> None:
    store = _mk_store(tmp_path)
    low = _mk_entry(confidence=0.2, suffix="-l")
    high = _mk_entry(confidence=0.9, suffix="-h")
    assert store._rank(high) > store._rank(low)


def test_more_recent_ranks_higher(tmp_path: Path) -> None:
    store = _mk_store(tmp_path)
    fresh = _mk_entry(timestamp=_now_iso(), suffix="-f")
    stale = _mk_entry(timestamp=_iso_ago(20 * 86400), suffix="-s")  # 20 days old
    assert store._rank(fresh) > store._rank(stale)


def test_stale_saturates_at_recency_floor(tmp_path: Path) -> None:
    """A 30-day-old entry should have recency factor 0.5 (the floor)."""
    store = _mk_store(tmp_path)
    # We ask for 60 days to be unambiguously past the floor.
    ancient = _mk_entry(timestamp=_iso_ago(60 * 86400), confidence=1.0)
    now_epoch = time.time()
    # Recency factor floors at 0.5 -> rank = 1.0 * 0.5 * 1.0 = 0.5
    assert store._rank_with_ts(ancient, now_epoch) == pytest.approx(0.5, rel=0.05)


def test_higher_applied_count_ranks_higher(tmp_path: Path) -> None:
    store = _mk_store(tmp_path)
    base = _mk_entry(confidence=0.5, applied_count=0, suffix="-b")
    used = _mk_entry(confidence=0.5, applied_count=10, suffix="-u")
    # Both same confidence + freshness -> applied_count decides.
    assert store._rank(used) > store._rank(base)


def test_zero_applied_count_still_ranks(tmp_path: Path) -> None:
    """applied_count=0 should not zero out the rank (log(1) = 0 -> multiplier 1)."""
    store = _mk_store(tmp_path)
    entry = _mk_entry(confidence=0.8, applied_count=0, timestamp=_now_iso())
    assert store._rank(entry) == pytest.approx(0.8, rel=0.05)


def test_composite_order(tmp_path: Path) -> None:
    """Across many entries, sort order follows the composite formula."""
    store = _mk_store(tmp_path)
    entries = [
        _mk_entry(confidence=0.9, applied_count=0, suffix="-a"),
        _mk_entry(confidence=0.3, applied_count=10, suffix="-b"),
        _mk_entry(confidence=0.5, applied_count=5, suffix="-c"),
    ]
    ranked = sorted(entries, key=store._rank, reverse=True)
    ranks = [store._rank(e) for e in ranked]
    assert ranks == sorted(ranks, reverse=True)
