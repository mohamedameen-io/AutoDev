"""v0.32.0 Phase 4.2: knowledge_lookup helper tests.

Covers :func:`orchestrator.knowledge_lookup.lookup_recent_failures` —
the time-bounded helper that the retry path calls before injecting the
``STUCK_CONTEXT`` block. Tests use a duck-typed stub store (no real
JSONL I/O) so the timeout-fallback case can mock an indefinitely-
awaiting store cleanly.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from config.defaults import default_config
from orchestrator.knowledge_lookup import lookup_recent_failures
from state.knowledge import KnowledgeEntry, KnowledgeStore


def _make_entry(
    text: str,
    *,
    event_type: str | None = "discard",
    task_id: str | None = None,
    task_signature: str | None = None,
    age_days: float = 0.0,
) -> KnowledgeEntry:
    """Build a KnowledgeEntry with controllable timestamp + metadata."""
    ts = datetime.now(timezone.utc) - timedelta(days=age_days)
    metadata: dict[str, Any] = {}
    if event_type is not None:
        metadata["event_type"] = event_type
    if task_id is not None:
        metadata["task_id"] = task_id
    if task_signature is not None:
        metadata["task_signature"] = task_signature
    return KnowledgeEntry(
        id=f"id-{abs(hash(text))%10_000}",
        timestamp=ts.isoformat(),
        role_source="critic_t",
        tier="swarm",
        text=text,
        confidence=0.5,
        metadata=metadata,
    )


class _StubStore:
    """Minimal duck-typed knowledge store for the lookup helper."""

    def __init__(self, entries: list[KnowledgeEntry]) -> None:
        self._entries = entries

    async def read_all(self, tier: str = "both") -> list[KnowledgeEntry]:
        # Filter to swarm tier to mirror KnowledgeStore semantics.
        if tier == "swarm":
            return [e for e in self._entries if e.tier == "swarm"]
        return list(self._entries)


class _StubOrch:
    def __init__(self, knowledge: Any) -> None:
        self.knowledge = knowledge


@pytest.mark.asyncio
async def test_lookup_recent_failures_empty() -> None:
    """No prior knowledge entries ⇒ empty list."""
    orch = _StubOrch(_StubStore([]))
    result = await lookup_recent_failures(orch, task_id="task-1")
    assert result == []


@pytest.mark.asyncio
async def test_lookup_recent_failures_finds_similar_task_discards() -> None:
    """Entries tagged with the same task_id surface in the result."""
    entries = [
        _make_entry(
            "EVENT: discard\nFAMILY: execute-phase\nHYPOTHESIS: foo broke",
            event_type="discard",
            task_id="task-42",
        ),
        _make_entry(
            "EVENT: soft_blocker\nFAMILY: execute-phase\nHYPOTHESIS: bar wedged",
            event_type="soft_blocker",
            task_id="task-42",
        ),
    ]
    orch = _StubOrch(_StubStore(entries))
    result = await lookup_recent_failures(orch, task_id="task-42")
    assert len(result) == 2
    assert any("foo broke" in s for s in result)
    assert any("bar wedged" in s for s in result)


@pytest.mark.asyncio
async def test_lookup_recent_failures_filters_by_age() -> None:
    """Entries older than the threshold are excluded."""
    fresh = _make_entry(
        "fresh discard text",
        event_type="discard",
        task_id="task-7",
        age_days=1.0,
    )
    stale = _make_entry(
        "stale discard text",
        event_type="discard",
        task_id="task-7",
        age_days=14.0,
    )
    orch = _StubOrch(_StubStore([fresh, stale]))
    result = await lookup_recent_failures(
        orch, task_id="task-7", threshold_days=7
    )
    assert any("fresh discard" in s for s in result)
    assert not any("stale discard" in s for s in result)


@pytest.mark.asyncio
async def test_lookup_recent_failures_timeout_fallback() -> None:
    """A slow KB query returns empty within the configured timeout."""

    class _SlowStore:
        async def read_all(self, tier: str = "both") -> list[KnowledgeEntry]:
            # Await indefinitely — wait_for must cancel us.
            await asyncio.Event().wait()
            return []

    orch = _StubOrch(_SlowStore())
    start = asyncio.get_event_loop().time()
    result = await lookup_recent_failures(
        orch, task_id="task-1", timeout_s=0.1
    )
    elapsed = asyncio.get_event_loop().time() - start
    assert result == []
    # Sanity: we did not block significantly past the timeout.
    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_lookup_recent_failures_respects_limit() -> None:
    """More than ``limit`` matches ⇒ exactly ``limit`` returned."""
    entries = [
        _make_entry(
            f"discard #{i}",
            event_type="discard",
            task_id="task-x",
            age_days=0.1 * i,
        )
        for i in range(10)
    ]
    orch = _StubOrch(_StubStore(entries))
    result = await lookup_recent_failures(orch, task_id="task-x", limit=3)
    assert len(result) == 3


@pytest.mark.asyncio
async def test_lookup_recent_failures_skips_irrelevant_event_types() -> None:
    """winner_promoted entries are not surfaced (positive signal, not failure)."""
    entries = [
        _make_entry("a discard", event_type="discard", task_id="t"),
        _make_entry(
            "a winner", event_type="winner_promoted", task_id="t"
        ),
    ]
    orch = _StubOrch(_StubStore(entries))
    result = await lookup_recent_failures(orch, task_id="t")
    assert any("a discard" in s for s in result)
    assert not any("a winner" in s for s in result)


@pytest.mark.asyncio
async def test_lookup_recent_failures_filters_other_task_signatures() -> None:
    """An entry tagged with a different task_signature is excluded."""
    entries = [
        _make_entry(
            "matching task entry",
            event_type="discard",
            task_id="task-A",
        ),
        _make_entry(
            "other task entry",
            event_type="discard",
            task_signature="some-other-signature",
        ),
    ]
    orch = _StubOrch(_StubStore(entries))
    result = await lookup_recent_failures(orch, task_id="task-A")
    assert any("matching task entry" in s for s in result)
    assert not any("other task entry" in s for s in result)


@pytest.mark.asyncio
async def test_lookup_handles_missing_knowledge_attr() -> None:
    """orch with no ``knowledge`` attribute ⇒ empty list (defensive)."""

    class _NoKnowledge:
        pass

    result = await lookup_recent_failures(_NoKnowledge(), task_id="task-1")
    assert result == []


@pytest.mark.asyncio
async def test_lookup_with_real_store(tmp_path: Path) -> None:
    """End-to-end with a real KnowledgeStore — record + lookup round-trip."""
    cfg = default_config()
    cfg.hive.enabled = False
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=tmp_path / "hive.jsonl")
    await store.record(
        "test discard lesson",
        role_source="critic_t",
        confidence=0.5,
        metadata={"event_type": "discard", "task_id": "task-real"},
    )

    class _Orch:
        def __init__(self, knowledge: KnowledgeStore) -> None:
            self.knowledge = knowledge

    result = await lookup_recent_failures(_Orch(store), task_id="task-real")
    assert any("test discard lesson" in s for s in result)
