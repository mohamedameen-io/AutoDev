"""v0.18.0 B1: lane-aware knowledge injection tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from config.defaults import default_config
from state.knowledge import KnowledgeStore, TournamentEvent


def _store(tmp_path: Path, lane_aware: bool = True) -> KnowledgeStore:
    cfg = default_config()
    cfg.knowledge.lane_aware_injection_enabled = lane_aware
    # Isolate from the user's real hive file: pin to a per-tmp path so
    # this test's injection ranking is not contaminated by pre-existing
    # global entries.
    cfg.hive.path = tmp_path / "hive.jsonl"
    return KnowledgeStore(cwd=tmp_path, cfg=cfg, hive_path=cfg.hive.path)


@pytest.mark.asyncio
async def test_record_tournament_event_persists_lane_metadata(tmp_path: Path) -> None:
    """TournamentEvent.lane is persisted to entry.metadata['lane']."""
    store = _store(tmp_path)
    event = TournamentEvent(
        event_type="winner_promoted",
        family="plan-tournament",
        hypothesis="hyp",
        evidence="placeholder lane-aware test evidence body long enough to clear the v0.35.0 C2 thin-evidence gate threshold cleanly",
        lane="distant-scout",
    )
    entry = await store.record_tournament_event(event)
    assert entry is not None
    assert entry.metadata.get("lane") == "distant-scout"


@pytest.mark.asyncio
async def test_record_tournament_event_no_lane_omits_metadata(tmp_path: Path) -> None:
    """When lane is None, metadata['lane'] is absent (universal lesson)."""
    store = _store(tmp_path)
    event = TournamentEvent(
        event_type="discard",
        family="plan-tournament",
        hypothesis="hyp",
        evidence="placeholder lane-aware test evidence body long enough to clear the v0.35.0 C2 thin-evidence gate threshold cleanly",
        # lane omitted → defaults to None
    )
    entry = await store.record_tournament_event(event)
    assert entry is not None
    assert "lane" not in entry.metadata


@pytest.mark.asyncio
async def test_inject_block_filters_by_lane(tmp_path: Path) -> None:
    """inject_block(lane='distant-scout') excludes lessons tagged with other lanes."""
    store = _store(tmp_path)
    # Use sufficiently distinct hypothesis + evidence + family text so the
    # bigram-Jaccard dedup doesn't merge them into a single entry.
    await store.record_tournament_event(TournamentEvent(
        event_type="winner_promoted",
        family="distant-scout-family",
        hypothesis="zzz unique distant abc xyz lesson 12345",
        evidence="distant evidence aaa bbb ccc 9876 distinct-distant-suffix 11ZZ22YY33XX44WW55VV66UU77TT88SS99RR",
        lane="distant-scout",
    ))
    await store.record_tournament_event(TournamentEvent(
        event_type="winner_promoted",
        family="local-tweak-family",
        hypothesis="qqq other local def ghi lesson 67890",
        evidence="local evidence ddd eee fff 5432 distinct-local-suffix AABBCCDDEEFFGGHHIIJJKKLLMMNNOOPPQQRRSSTT",
        lane="local-tweak",
    ))
    await store.record_tournament_event(TournamentEvent(
        event_type="winner_promoted",
        family="universal-family",
        hypothesis="rrr universal mno pqr lesson 24680",
        evidence="universal evidence ggg hhh iii 1357 distinct-universal-suffix UUVVWWXXYYZZ00112233445566778899ABCDEF",
        # no lane → universal
    ))

    block = await store.inject_block("developer", lane="distant-scout")
    # distant-scout entry survives
    assert "distant-scout-family" in block
    # universal entry survives
    assert "universal-family" in block
    # local-tweak entry filtered out
    assert "local-tweak-family" not in block


@pytest.mark.asyncio
async def test_inject_block_no_lane_argument_includes_all(tmp_path: Path) -> None:
    """When inject_block is called without lane, no filter is applied."""
    store = _store(tmp_path)
    await store.record_tournament_event(TournamentEvent(
        event_type="winner_promoted",
        family="distant-scout-family",
        hypothesis="zzz unique distant abc xyz lesson 12345",
        evidence="distant evidence aaa bbb ccc 9876 distinct-distant-suffix 11ZZ22YY33XX44WW55VV66UU77TT88SS99RR",
        lane="distant-scout",
    ))
    await store.record_tournament_event(TournamentEvent(
        event_type="winner_promoted",
        family="local-tweak-family",
        hypothesis="qqq other local def ghi lesson 67890",
        evidence="local evidence ddd eee fff 5432 distinct-local-suffix AABBCCDDEEFFGGHHIIJJKKLLMMNNOOPPQQRRSSTT",
        lane="local-tweak",
    ))

    block = await store.inject_block("developer")
    assert "distant-scout-family" in block
    assert "local-tweak-family" in block


@pytest.mark.asyncio
async def test_inject_block_lane_aware_disabled(tmp_path: Path) -> None:
    """When lane_aware_injection_enabled=False, lane argument is ignored."""
    store = _store(tmp_path, lane_aware=False)
    await store.record_tournament_event(TournamentEvent(
        event_type="winner_promoted",
        family="distant-scout-family",
        hypothesis="zzz unique distant abc xyz lesson 12345",
        evidence="distant evidence aaa bbb ccc 9876 distinct-distant-suffix 11ZZ22YY33XX44WW55VV66UU77TT88SS99RR",
        lane="distant-scout",
    ))
    await store.record_tournament_event(TournamentEvent(
        event_type="winner_promoted",
        family="local-tweak-family",
        hypothesis="qqq other local def ghi lesson 67890",
        evidence="local evidence ddd eee fff 5432 distinct-local-suffix AABBCCDDEEFFGGHHIIJJKKLLMMNNOOPPQQRRSSTT",
        lane="local-tweak",
    ))

    # With the toggle off, even passing a lane includes all entries.
    block = await store.inject_block("developer", lane="distant-scout")
    assert "distant-scout-family" in block
    assert "local-tweak-family" in block


@pytest.mark.asyncio
async def test_universal_lessons_always_injected(tmp_path: Path) -> None:
    """Lane-less lessons are universal and pass through any lane filter."""
    store = _store(tmp_path)
    await store.record_tournament_event(TournamentEvent(
        event_type="winner_promoted",
        family="plan-tournament",
        hypothesis="universal-lesson",
        evidence="placeholder lane-aware test evidence body long enough to clear the v0.35.0 C2 thin-evidence gate threshold cleanly",
    ))

    for lane in ("distant-scout", "local-tweak", "architectural"):
        block = await store.inject_block("developer", lane=lane)
        assert "universal-lesson" in block, f"missing for lane={lane}"
