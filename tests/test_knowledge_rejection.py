"""Rejection: moves lessons to rejected_lessons.jsonl and blocks re-learning."""

from __future__ import annotations

from pathlib import Path

import pytest

from config.defaults import default_config
from state.knowledge import KnowledgeStore
from state.paths import knowledge_path, rejected_lessons_path


def _mk_store(tmp_path: Path) -> KnowledgeStore:
    cfg = default_config()
    cfg.hive.enabled = False
    return KnowledgeStore(tmp_path, cfg=cfg, hive_path=tmp_path / "hive.jsonl")


@pytest.mark.asyncio
async def test_reject_moves_lesson_to_rejection_log(tmp_path: Path) -> None:
    store = _mk_store(tmp_path)
    entry = await store.record(
        "use filelock for cross-process coordination every time",
        role_source="developer",
    )
    assert entry is not None

    await store.reject(entry.id, "not applicable in this project")

    # Swarm no longer contains it.
    swarm = await store.read_all(tier="swarm")
    assert all(e.id != entry.id for e in swarm)

    # Rejection log contains it with the reason.
    rejected = await store.read_rejected()
    assert len(rejected) == 1
    assert rejected[0].id == entry.id
    assert rejected[0].reason == "not applicable in this project"
    # rejected_at is an ISO timestamp.
    assert rejected[0].rejected_at


@pytest.mark.asyncio
async def test_subsequent_similar_record_is_blocked(tmp_path: Path) -> None:
    store = _mk_store(tmp_path)
    entry = await store.record(
        "always use filelock for cross process coordination",
        role_source="developer",
    )
    assert entry is not None
    await store.reject(entry.id, "prefer asyncio.Lock")

    # Very similar text should be blocked by rejection.
    blocked = await store.record(
        "always use filelock for cross-process coordination now",
        role_source="developer",
    )
    assert blocked is None

    # Swarm remains empty.
    swarm = await store.read_all(tier="swarm")
    assert swarm == []


@pytest.mark.asyncio
async def test_rejection_is_persistent(tmp_path: Path) -> None:
    """A fresh store instance still sees the rejection list."""
    store1 = _mk_store(tmp_path)
    entry = await store1.record("reject this pattern please", role_source="developer")
    assert entry is not None
    await store1.reject(entry.id, "bad pattern")

    store2 = _mk_store(tmp_path)
    blocked = await store2.record(
        "reject this pattern please",
        role_source="developer",
    )
    assert blocked is None


@pytest.mark.asyncio
async def test_rejection_paths_are_absolute(tmp_path: Path) -> None:
    """Both knowledge and rejection files live under the absolute .autodev dir."""
    store = _mk_store(tmp_path)
    entry = await store.record("pattern-xyz-123 something here", role_source="developer")
    assert entry is not None
    await store.reject(entry.id, "mistake")

    kp = knowledge_path(tmp_path)
    rp = rejected_lessons_path(tmp_path)
    # The path helpers return absolute paths when given an absolute tmp_path.
    assert kp.is_absolute()
    assert rp.is_absolute()
    assert rp.exists()


@pytest.mark.asyncio
async def test_reject_unknown_id_is_noop(tmp_path: Path) -> None:
    """Rejecting an unknown id is a safe no-op."""
    store = _mk_store(tmp_path)
    # Nothing to reject yet.
    await store.reject("no-such-id", "nothing to do")
    rejected = await store.read_rejected()
    assert rejected == []


@pytest.mark.asyncio
async def test_dissimilar_lesson_not_blocked_after_rejection(tmp_path: Path) -> None:
    """Rejection must not block unrelated lessons."""
    store = _mk_store(tmp_path)
    entry = await store.record(
        "always use filelock for cross-process coordination",
        role_source="developer",
    )
    assert entry is not None
    await store.reject(entry.id, "no")

    ok = await store.record(
        "XYZ QWERTY 12345 totally unrelated disjoint text here",
        role_source="developer",
    )
    assert ok is not None
