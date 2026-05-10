"""Phase 2 (anti-bloat): seed-pack loader idempotency tests.

Two paths to idempotency are exercised:

1. The marker file under ``<cwd>/.autodev/seed_packs.json`` short-circuits
   re-loads of the same pack.
2. Even with the marker deleted, the bigram-Jaccard dedup against existing
   hive contents prevents duplicate insertion.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from config.defaults import default_config
from state.knowledge import KnowledgeStore
from state.seed_packs import seed_pack_if_missing


REPO_ROOT = Path(__file__).resolve().parents[1]
PACK_PATH = REPO_ROOT / "seeds" / "anti_bloat_v1.jsonl"


def _store(tmp_path: Path) -> KnowledgeStore:
    """Real KnowledgeStore with hive redirected into ``tmp_path`` so tests
    cannot pollute the user's real ~/.local/share/autodev/ hive."""
    cfg = default_config()
    return KnowledgeStore(
        cwd=tmp_path,
        cfg=cfg,
        hive_path=tmp_path / "hive.jsonl",
    )


def _hive_line_count(hive_file: Path) -> int:
    if not hive_file.exists():
        return 0
    return sum(1 for ln in hive_file.read_text().splitlines() if ln.strip())


@pytest.mark.asyncio
async def test_seed_pack_inserts_on_first_call(tmp_path: Path) -> None:
    """First call inserts every entry from the pack; marker is written."""
    store = _store(tmp_path)
    marker_dir = tmp_path / ".autodev"
    inserted = await seed_pack_if_missing(
        store, PACK_PATH, "anti_bloat_v1", marker_dir=marker_dir
    )
    assert inserted == 18
    assert _hive_line_count(tmp_path / "hive.jsonl") == 18

    marker = marker_dir / "seed_packs.json"
    assert marker.exists()
    data = json.loads(marker.read_text())
    assert "anti_bloat_v1" in data


@pytest.mark.asyncio
async def test_seed_pack_marker_short_circuits_second_call(tmp_path: Path) -> None:
    """Second call with marker present is a no-op (returns 0)."""
    store = _store(tmp_path)
    marker_dir = tmp_path / ".autodev"
    first = await seed_pack_if_missing(
        store, PACK_PATH, "anti_bloat_v1", marker_dir=marker_dir
    )
    assert first == 18

    second = await seed_pack_if_missing(
        store, PACK_PATH, "anti_bloat_v1", marker_dir=marker_dir
    )
    assert second == 0
    assert _hive_line_count(tmp_path / "hive.jsonl") == 18  # unchanged


@pytest.mark.asyncio
async def test_seed_pack_jaccard_dedup_when_marker_lost(tmp_path: Path) -> None:
    """Marker deletion does not produce duplicates — Jaccard dedup catches them."""
    store = _store(tmp_path)
    marker_dir = tmp_path / ".autodev"
    await seed_pack_if_missing(store, PACK_PATH, "anti_bloat_v1", marker_dir=marker_dir)

    marker = marker_dir / "seed_packs.json"
    marker.unlink()

    # Re-seed with the marker gone; the existing hive entries must dedup
    # every candidate via the underlying jaccard check.
    inserted = await seed_pack_if_missing(
        store, PACK_PATH, "anti_bloat_v1", marker_dir=marker_dir
    )
    assert inserted == 0
    assert _hive_line_count(tmp_path / "hive.jsonl") == 18


@pytest.mark.asyncio
async def test_seed_pack_missing_file_is_noop(tmp_path: Path) -> None:
    """Missing pack file returns 0 without raising."""
    store = _store(tmp_path)
    marker_dir = tmp_path / ".autodev"
    inserted = await seed_pack_if_missing(
        store,
        tmp_path / "does_not_exist.jsonl",
        "ghost_pack",
        marker_dir=marker_dir,
    )
    assert inserted == 0
    assert _hive_line_count(tmp_path / "hive.jsonl") == 0


@pytest.mark.asyncio
async def test_seed_pack_skipped_when_hive_disabled(tmp_path: Path) -> None:
    """Operator-disabled hive (cfg.hive.enabled=False) short-circuits."""
    cfg = default_config()
    cfg.hive.enabled = False
    store = KnowledgeStore(
        cwd=tmp_path,
        cfg=cfg,
        hive_path=tmp_path / "hive.jsonl",
    )
    marker_dir = tmp_path / ".autodev"
    inserted = await seed_pack_if_missing(
        store, PACK_PATH, "anti_bloat_v1", marker_dir=marker_dir
    )
    assert inserted == 0
    assert _hive_line_count(tmp_path / "hive.jsonl") == 0
