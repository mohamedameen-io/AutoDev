"""KnowledgeStore.record / read_all / dedup / cap enforcement."""

from __future__ import annotations

from pathlib import Path

import pytest

from config.defaults import default_config
from state.knowledge import KnowledgeStore


@pytest.mark.asyncio
async def test_fresh_store_is_empty(tmp_path: Path) -> None:
    cfg = default_config()
    cfg.hive.enabled = False  # isolate from real hive path
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=tmp_path / "hive.jsonl")
    entries = await store.read_all()
    assert entries == []


@pytest.mark.asyncio
async def test_record_single_lesson_is_readable(tmp_path: Path) -> None:
    cfg = default_config()
    cfg.hive.enabled = False
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=tmp_path / "hive.jsonl")
    entry = await store.record(
        "prefer atomic writes via tmp-then-replace for crash safety",
        role_source="developer",
        confidence=0.5,
    )
    assert entry is not None
    entries = await store.read_all(tier="swarm")
    assert len(entries) == 1
    assert entries[0].id == entry.id
    assert "atomic writes" in entries[0].text


@pytest.mark.asyncio
async def test_dict_form_compat(tmp_path: Path) -> None:
    """Phase-4 dict form still works (``record({"lesson": "...", "role": "..."})``)."""
    cfg = default_config()
    cfg.hive.enabled = False
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=tmp_path / "hive.jsonl")
    entry = await store.record(
        {
            "lesson": "always check return codes from subprocess.run",
            "role": "developer",
            "confidence": 0.6,
            "metadata": {"source": "phase4"},
        }
    )
    assert entry is not None
    assert entry.role_source == "developer"
    assert entry.confidence == pytest.approx(0.6)
    assert entry.metadata.get("source") == "phase4"


@pytest.mark.asyncio
async def test_duplicate_is_merged(tmp_path: Path) -> None:
    cfg = default_config()
    cfg.hive.enabled = False
    cfg.knowledge.dedup_threshold = 0.6
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=tmp_path / "hive.jsonl")
    first = await store.record(
        "always prefer async file locks over busy-polling loops",
        role_source="developer",
    )
    second = await store.record(
        "always prefer async file locks over busy polling loops",
        role_source="developer",
    )
    assert first is not None and second is not None
    # Same id: merged.
    assert first.id == second.id
    entries = await store.read_all(tier="swarm")
    assert len(entries) == 1
    # Confirmations bumped.
    assert entries[0].confirmations >= 2
    # Confidence bumped (we added +0.1 per merge).
    assert entries[0].confidence > first.confidence


@pytest.mark.asyncio
async def test_dissimilar_yields_two_entries(tmp_path: Path) -> None:
    cfg = default_config()
    cfg.hive.enabled = False
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=tmp_path / "hive.jsonl")
    await store.record(
        "always run tests before committing a change",
        role_source="developer",
    )
    await store.record(
        "XYZ QWERTY 12345 disjoint vocabulary here",
        role_source="developer",
    )
    entries = await store.read_all(tier="swarm")
    assert len(entries) == 2


@pytest.mark.asyncio
async def test_swarm_cap_evicts_lowest_ranked(tmp_path: Path) -> None:
    """When the swarm exceeds ``swarm_max_entries`` the lowest-ranked entry is evicted."""
    cfg = default_config()
    cfg.hive.enabled = False
    cfg.knowledge.swarm_max_entries = 3
    # Also disable promotion side-effects that could interact with cap.
    cfg.knowledge.promotion_min_confirmations = 99
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=tmp_path / "hive.jsonl")

    # Record 3 entries with increasing confidence so the oldest/lowest is evictable.
    low = await store.record(
        "aaaa bbbb cccc dddd eeee ffff gggg",
        role_source="developer",
        confidence=0.1,
    )
    mid = await store.record(
        "hhhh iiii jjjj kkkk llll mmmm nnnn",
        role_source="developer",
        confidence=0.5,
    )
    high = await store.record(
        "oooo pppp qqqq rrrr ssss tttt uuuu",
        role_source="developer",
        confidence=0.9,
    )
    assert low and mid and high

    entries = await store.read_all(tier="swarm")
    assert len(entries) == 3

    # Add a 4th — cap=3 triggers eviction of the lowest-ranked.
    extra = await store.record(
        "vvvv wwww xxxx yyyy zzzz 1111 2222",
        role_source="developer",
        confidence=0.7,
    )
    assert extra is not None

    entries_after = await store.read_all(tier="swarm")
    assert len(entries_after) == 3
    remaining_ids = {e.id for e in entries_after}
    # The lowest-ranked (confidence=0.1, freshly created so recency ~1.0) should
    # be evicted — rank = 0.1 * 1 * 1 = 0.1, strictly the smallest.
    assert low.id not in remaining_ids
    # The high-confidence entry survives.
    assert high.id in remaining_ids
