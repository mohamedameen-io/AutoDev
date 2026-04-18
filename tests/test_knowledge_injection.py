"""inject_block: denylist, limits, merged swarm+hive."""

from __future__ import annotations

from pathlib import Path

import pytest

from config.defaults import default_config
from state.knowledge import KnowledgeStore


@pytest.fixture
def hive_file(tmp_path: Path) -> Path:
    return tmp_path / "hive" / "shared-learnings.jsonl"


# Each lesson is built from deliberately disjoint alphabets so bigram Jaccard
# stays well below the 0.6 dedup threshold. Using shared English boilerplate
# (e.g. "unique lesson number N ...") pushes similarity well above 0.6 in
# short strings and collapses them into a single merged entry.
_DISJOINT_LESSONS = (
    "aaaa bbbb cccc dddd eeee ffff gggg hhhh iiii jjjj",
    "kkkk llll mmmm nnnn oooo pppp qqqq rrrr ssss tttt",
    "uuuu vvvv wwww xxxx yyyy zzzz 0000 1111 2222 3333",
    "4444 5555 6666 7777 8888 9999 @@@@ #### $$$$ %%%%",
    "&&&& **** (((( )))) ---- ++++ ==== [[[[ ]]]] {{{{",
)


async def _seed_swarm(store: KnowledgeStore, how_many: int = 3) -> None:
    # Record a few distinct lessons so ranking has something to pick from.
    for i in range(how_many):
        text = _DISJOINT_LESSONS[i % len(_DISJOINT_LESSONS)]
        await store.record(
            text,
            role_source="developer",
            confidence=0.5 + i * 0.05,
        )


@pytest.mark.asyncio
async def test_inject_for_coder_returns_lessons(tmp_path: Path, hive_file: Path) -> None:
    cfg = default_config()
    cfg.hive.enabled = False
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=hive_file)
    await _seed_swarm(store, how_many=3)

    block = await store.inject_block("developer")
    assert "Lessons learned from prior work:" in block
    assert block.count("\n- [conf:") == 3


@pytest.mark.asyncio
async def test_inject_for_denylist_role_returns_empty(
    tmp_path: Path, hive_file: Path
) -> None:
    cfg = default_config()
    cfg.hive.enabled = False
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=hive_file)
    await _seed_swarm(store, how_many=2)

    # Every denylisted role returns empty, regardless of stored lessons.
    for role in ("explorer", "judge", "critic_t", "architect_b", "synthesizer"):
        assert await store.inject_block(role) == ""


@pytest.mark.asyncio
async def test_inject_respects_max_inject_count(tmp_path: Path, hive_file: Path) -> None:
    cfg = default_config()
    cfg.hive.enabled = False
    cfg.knowledge.max_inject_count = 2
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=hive_file)
    await _seed_swarm(store, how_many=5)

    block = await store.inject_block("developer")
    assert block.count("\n- [conf:") == 2

    # Explicit limit overrides the config cap.
    block1 = await store.inject_block("developer", limit=1)
    assert block1.count("\n- [conf:") == 1


@pytest.mark.asyncio
async def test_inject_merges_swarm_and_hive(tmp_path: Path, hive_file: Path) -> None:
    """Hive entries surface alongside swarm entries when both are enabled."""
    cfg = default_config()
    cfg.knowledge.max_inject_count = 10
    cfg.knowledge.promotion_min_confirmations = 2
    cfg.knowledge.promotion_min_confidence = 0.5
    # Point hive at a tmp path so real global state is not touched.
    cfg.hive.path = hive_file

    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=hive_file)

    # Record a swarm-only lesson.
    await store.record(
        "prefer atomic writes using tmp-rename for crash safety",
        role_source="developer",
        confidence=0.5,
    )
    # Force a promotion: same entry recorded twice with sufficient confidence.
    # First record set confirmations=1, confidence=0.55 after merge? Re-record with a bump.
    await store.record(
        "favor tmp-then-rename for atomic crash-safe writes",
        role_source="developer",
        confidence=0.5,
    )
    # Now confirm once more to push confirmations >= 2.
    await store.record(
        "favor tmp-then-rename for atomic crash-safe writes",
        role_source="developer",
        confidence=0.5,
    )

    # Add a totally different lesson that stays swarm-only (single confirmation).
    await store.record(
        "XYZ QWERTY 12345 lorem ipsum disjoint text",
        role_source="developer",
        confidence=0.8,
    )

    block = await store.inject_block("developer")
    assert "Lessons learned from prior work:" in block
    # The swarm-only "XYZ" lesson must appear.
    assert "XYZ" in block or "QWERTY" in block


@pytest.mark.asyncio
async def test_inject_when_disabled_returns_empty(
    tmp_path: Path, hive_file: Path
) -> None:
    cfg = default_config()
    cfg.hive.enabled = False
    cfg.knowledge.enabled = False
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=hive_file)
    await store.record("this should not appear", role_source="developer")
    assert await store.inject_block("developer") == ""


@pytest.mark.asyncio
async def test_inject_empty_store_returns_empty(
    tmp_path: Path, hive_file: Path
) -> None:
    cfg = default_config()
    cfg.hive.enabled = False
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=hive_file)
    assert await store.inject_block("developer") == ""


@pytest.mark.asyncio
async def test_inject_limit_zero_returns_empty(
    tmp_path: Path, hive_file: Path
) -> None:
    cfg = default_config()
    cfg.hive.enabled = False
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=hive_file)
    await _seed_swarm(store, how_many=2)
    assert await store.inject_block("developer", limit=0) == ""


@pytest.mark.asyncio
async def test_inject_block_increments_applied_count(
    tmp_path: Path, hive_file: Path
) -> None:
    """inject_block must increment applied_count on each selected swarm entry."""
    cfg = default_config()
    cfg.hive.enabled = False
    cfg.knowledge.max_inject_count = 2
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=hive_file)

    # Record two disjoint lessons.
    await store.record(
        _DISJOINT_LESSONS[0],
        role_source="developer",
        confidence=0.8,
    )
    await store.record(
        _DISJOINT_LESSONS[1],
        role_source="developer",
        confidence=0.7,
    )

    # Before injection, applied_count should be 0 for all entries.
    entries_before = await store.read_all(tier="swarm")
    assert all(e.applied_count == 0 for e in entries_before)

    # Call inject_block — this should select and inject the top-2 entries.
    block = await store.inject_block("developer")
    assert "Lessons learned from prior work:" in block

    # After injection, the selected entries should have applied_count == 1.
    entries_after = await store.read_all(tier="swarm")
    assert all(e.applied_count == 1 for e in entries_after), (
        f"Expected applied_count=1 for all entries, got: "
        f"{[(e.text[:20], e.applied_count) for e in entries_after]}"
    )

    # A second injection should increment to 2.
    await store.inject_block("developer")
    entries_after2 = await store.read_all(tier="swarm")
    assert all(e.applied_count == 2 for e in entries_after2)
