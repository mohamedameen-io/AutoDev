"""Phase 2 (anti-bloat): seeded entries flow through lane-aware injection.

Seeded entries carry ``metadata.lane = "anti_bloat"``. The existing
:meth:`KnowledgeStore.inject_block` lane-aware filter (``_lane_match``)
ships universal lessons (no lane) to consumers regardless of their lane;
lane-tagged lessons match only when the consumer requests the same lane,
OR when the consumer passes ``lane=None`` (no filter applied at all).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config.defaults import default_config
from state.knowledge import KnowledgeStore
from state.seed_packs import seed_pack_if_missing


REPO_ROOT = Path(__file__).resolve().parents[1]
PACK_PATH = REPO_ROOT / "seeds" / "anti_bloat_v1.jsonl"


def _store(tmp_path: Path) -> KnowledgeStore:
    cfg = default_config()
    return KnowledgeStore(
        cwd=tmp_path,
        cfg=cfg,
        hive_path=tmp_path / "hive.jsonl",
    )


@pytest.mark.asyncio
async def test_seeded_lessons_visible_to_anti_bloat_lane(tmp_path: Path) -> None:
    """Reviewer asking for ``lane='anti_bloat'`` sees seeded entries."""
    store = _store(tmp_path)
    inserted = await seed_pack_if_missing(
        store, PACK_PATH, "anti_bloat_v1", marker_dir=tmp_path / ".autodev"
    )
    assert inserted > 0

    block = await store.inject_block("reviewer", lane="anti_bloat")
    assert block, "expected a non-empty lessons block for the reviewer"
    # At least one seeded entry's distinctive phrase should appear. We pick
    # phrases that are stable across the rendered ``[conf:0.85] <text>`` form.
    assert "abstractions" in block or "magic numbers" in block or "anti_bloat" in block.lower() or "abstraction" in block


@pytest.mark.asyncio
async def test_seeded_lessons_visible_when_lane_is_none(tmp_path: Path) -> None:
    """Reviewer with no lane filter (``lane=None``) still sees seeded entries.

    With ``lane=None`` the existing ``inject_block`` skips the lane filter
    entirely, so every entry — including the lane-tagged seeded ones —
    is eligible.
    """
    store = _store(tmp_path)
    await seed_pack_if_missing(
        store, PACK_PATH, "anti_bloat_v1", marker_dir=tmp_path / ".autodev"
    )

    block = await store.inject_block("reviewer", lane=None)
    assert block, "expected a non-empty lessons block when lane is None"
    # Sanity: same coverage as the lane-specific case.
    assert "abstractions" in block or "abstraction" in block or "magic numbers" in block


@pytest.mark.asyncio
async def test_seeded_lessons_filtered_out_for_unrelated_lane(tmp_path: Path) -> None:
    """A consumer asking for an unrelated lane does NOT see anti_bloat seeds.

    The lane-aware filter excludes entries whose ``metadata.lane`` differs
    from the consumer's lane. Universal (no-lane) entries would still pass,
    but seeded anti_bloat entries are tagged so they should be filtered out.
    """
    store = _store(tmp_path)
    await seed_pack_if_missing(
        store, PACK_PATH, "anti_bloat_v1", marker_dir=tmp_path / ".autodev"
    )

    block = await store.inject_block("reviewer", lane="distant-scout")
    # Block may be empty or non-empty (depends on other entries) — but it
    # must NOT contain the distinctive seeded phrasing about abstractions
    # for a single call site.
    assert "single call site" not in block
    assert "BaseClass, AbstractFactory" not in block


@pytest.mark.asyncio
async def test_denylist_role_still_blocked_for_seeded_lessons(tmp_path: Path) -> None:
    """Stateless/fact-finding roles on the denylist see no seeded lessons either."""
    store = _store(tmp_path)
    await seed_pack_if_missing(
        store, PACK_PATH, "anti_bloat_v1", marker_dir=tmp_path / ".autodev"
    )

    # ``explorer`` is in the default denylist (KnowledgeConfig.denylist_roles).
    block = await store.inject_block("explorer", lane="anti_bloat")
    assert block == ""
