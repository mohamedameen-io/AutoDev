"""Hive promotion: confirmations + confidence thresholds + idempotency."""

from __future__ import annotations

from pathlib import Path

import pytest

from config.defaults import default_config
from state.knowledge import KnowledgeStore


def _mk_cfg(
    *,
    min_confirmations: int = 3,
    min_confidence: float = 0.7,
    dedup: float = 0.6,
):
    cfg = default_config()
    cfg.knowledge.promotion_min_confirmations = min_confirmations
    cfg.knowledge.promotion_min_confidence = min_confidence
    cfg.knowledge.dedup_threshold = dedup
    return cfg


@pytest.mark.asyncio
async def test_promotes_after_three_confirmations_high_confidence(
    tmp_path: Path,
) -> None:
    cfg = _mk_cfg(min_confirmations=3, min_confidence=0.7)
    cfg.hive.path = tmp_path / "hive.jsonl"
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=cfg.hive.path)

    text = "always run the full test suite before committing any change"
    # Start confidence 0.8 -> plenty of headroom for the 0.7 threshold.
    await store.record(text, role_source="developer", confidence=0.8)
    await store.record(text, role_source="developer", confidence=0.8)
    await store.record(text, role_source="developer", confidence=0.8)

    hive_entries = await store.read_all(tier="hive")
    assert len(hive_entries) == 1
    assert hive_entries[0].tier == "hive"
    assert "full test suite" in hive_entries[0].text


@pytest.mark.asyncio
async def test_two_confirmations_is_not_enough(tmp_path: Path) -> None:
    cfg = _mk_cfg(min_confirmations=3, min_confidence=0.7)
    cfg.hive.path = tmp_path / "hive.jsonl"
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=cfg.hive.path)

    text = "always run the full test suite before committing any change"
    await store.record(text, role_source="developer", confidence=0.8)
    await store.record(text, role_source="developer", confidence=0.8)

    hive_entries = await store.read_all(tier="hive")
    assert hive_entries == []


@pytest.mark.asyncio
async def test_low_confidence_is_not_promoted(tmp_path: Path) -> None:
    """Even 5 confirmations should not promote a low-confidence lesson."""
    cfg = _mk_cfg(min_confirmations=3, min_confidence=0.7)
    cfg.hive.path = tmp_path / "hive.jsonl"
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=cfg.hive.path)

    text = "maybe try foo bar baz qux quux corge grault garply"
    for _ in range(5):
        # Start very low; even +0.1 per merge won't clear 0.7.
        await store.record(text, role_source="developer", confidence=0.1)

    hive_entries = await store.read_all(tier="hive")
    assert hive_entries == []


@pytest.mark.asyncio
async def test_promotion_is_idempotent(tmp_path: Path) -> None:
    """Re-running promotion (further confirmations) must not duplicate the hive entry."""
    cfg = _mk_cfg(min_confirmations=3, min_confidence=0.7)
    cfg.hive.path = tmp_path / "hive.jsonl"
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=cfg.hive.path)

    text = "always use tmp-then-rename for atomic writes; never write in place"
    for _ in range(6):
        await store.record(text, role_source="developer", confidence=0.8)

    hive_entries = await store.read_all(tier="hive")
    assert len(hive_entries) == 1


@pytest.mark.asyncio
async def test_promotion_disabled_when_hive_off(tmp_path: Path) -> None:
    cfg = _mk_cfg(min_confirmations=2, min_confidence=0.7)
    cfg.hive.enabled = False  # master switch off
    cfg.hive.path = tmp_path / "hive.jsonl"
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=cfg.hive.path)

    text = "some perfectly good lesson about locks and filelock usage"
    await store.record(text, role_source="developer", confidence=0.9)
    await store.record(text, role_source="developer", confidence=0.9)

    hive_entries = await store.read_all(tier="hive")
    assert hive_entries == []


@pytest.mark.asyncio
async def test_promotion_disabled_when_knowledge_hive_off(tmp_path: Path) -> None:
    cfg = _mk_cfg(min_confirmations=2, min_confidence=0.7)
    cfg.knowledge.hive_enabled = False
    cfg.hive.path = tmp_path / "hive.jsonl"
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=cfg.hive.path)

    text = "some perfectly good lesson about locks and filelock usage"
    await store.record(text, role_source="developer", confidence=0.9)
    await store.record(text, role_source="developer", confidence=0.9)

    hive_entries = await store.read_all(tier="hive")
    assert hive_entries == []
