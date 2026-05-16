"""v0.35.0 integration: repeated failed entry quarantines and stops injecting.

Replays the v0.32.0 fixture's pollution pattern: a single low-yield
knowledge entry that has been injected over and over without ever
preceding a successful task. Asserts that the soft-flag quarantine
trips by the 11th inject_block call, the JSONL audit line is written
once, and subsequent calls no longer increment applied_count (because
the entry is filtered out of the rotation).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from config.defaults import default_config
from state.knowledge import KnowledgeStore
from state.paths import knowledge_path


@pytest.mark.asyncio
async def test_repeated_failed_entry_quarantines_and_stops_injecting(
    tmp_path: Path,
) -> None:
    cfg = default_config()
    cfg.hive.enabled = False
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=tmp_path / "hive.jsonl")

    # Single entry with a wholly-disjoint character set so dedup never
    # merges anything else into it during the test run.
    seeded = await store.record(
        "aaaa bbbb cccc dddd eeee ffff gggg hhhh iiii jjjj",
        role_source="developer",
        confidence=0.5,
    )
    assert seeded is not None
    seeded_id = seeded.id

    # 12 inject calls. By call 11 the threshold trips
    # (applied_count > 10 ∧ ratio < 0.1); call 12 must observe the
    # entry as quarantined and skip incrementing.
    for _ in range(12):
        await store.inject_block("developer")

    entries = await store.read_all(tier="swarm")
    assert len(entries) == 1
    surviving = entries[0]
    assert surviving.id == seeded_id
    # The 11th call performed the increment-then-flip atomically: the
    # 11th increment landed (count 10 -> 11) AND quarantined=True was
    # set in the same write. The 12th call observed the flag and did
    # NOT increment, so the final count is exactly 11.
    assert surviving.applied_count == 11
    assert surviving.quarantined is True
    assert surviving.succeeded_after_count == 0

    audit_path = knowledge_path(tmp_path).parent / "quarantine_audit.jsonl"
    assert audit_path.exists()
    lines = [ln for ln in audit_path.read_text().splitlines() if ln.strip()]
    # Exactly one decision recorded — the flip is one-shot.
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["entry_id"] == seeded_id
    assert record["applied_count"] == 11
    assert record["succeeded_after_count"] == 0
    assert record["reason"] == "applied_threshold_no_success"
