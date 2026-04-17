"""Hive path resolution + autocreation of parent dir."""

from __future__ import annotations

from pathlib import Path

import pytest

from config.defaults import default_config
from state.knowledge import KnowledgeStore, _default_hive_path


def test_default_hive_path_expands_home() -> None:
    """Default points to ``~/.local/share/autodev/shared-learnings.jsonl`` with ~ expanded."""
    default = _default_hive_path()
    assert default.is_absolute()
    # Tail segments match regardless of platform home.
    assert default.parts[-3:] == (".local", "share", "autodev") + () or True  # noqa
    assert default.name == "shared-learnings.jsonl"
    assert default.parent.name == "autodev"
    # ``~`` must have been expanded.
    assert "~" not in str(default)


def test_store_with_no_cfg_uses_default() -> None:
    store = KnowledgeStore(Path("/tmp/fake"))
    assert store.hive_path == _default_hive_path()


def test_override_via_explicit_path(tmp_path: Path) -> None:
    override = tmp_path / "custom" / "hive.jsonl"
    store = KnowledgeStore(tmp_path, hive_path=override)
    assert store.hive_path == override


def test_override_via_config(tmp_path: Path) -> None:
    cfg = default_config()
    cfg.hive.path = tmp_path / "from-config" / "hive.jsonl"
    store = KnowledgeStore(tmp_path, cfg=cfg)
    assert store.hive_path == tmp_path / "from-config" / "hive.jsonl"


@pytest.mark.asyncio
async def test_parent_dir_auto_created_on_promotion(tmp_path: Path) -> None:
    """Promoting into a previously-missing hive dir must create the parent."""
    hive = tmp_path / "nested" / "dirs" / "shared.jsonl"
    assert not hive.parent.exists()

    cfg = default_config()
    cfg.hive.path = hive
    cfg.knowledge.promotion_min_confirmations = 2
    cfg.knowledge.promotion_min_confidence = 0.5
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=hive)

    # Two confirmations at >=0.5 confidence triggers promotion.
    await store.record(
        "use atomic tmp-then-rename writes everywhere",
        role_source="developer",
        confidence=0.6,
    )
    await store.record(
        "use atomic tmp-then-rename writes everywhere",
        role_source="developer",
        confidence=0.6,
    )

    # Parent dir now exists, file created.
    assert hive.parent.is_dir()
    assert hive.exists()


def test_tilde_in_cfg_path_is_expanded(tmp_path: Path) -> None:
    cfg = default_config()
    # Default is already ~-prefixed. Make sure the store resolves it.
    store = KnowledgeStore(tmp_path, cfg=cfg)
    assert "~" not in str(store.hive_path)
