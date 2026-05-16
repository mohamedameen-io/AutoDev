"""v0.35.0 Tier C — knowledge-base hygiene unit tests.

Covers:
* C1 quarantine soft-flag + audit JSONL trail (incl. legacy-entry safety).
* C1 prerequisite: succeeded_after_count is actually incremented on
  task success via :meth:`KnowledgeStore.credit_task_success`.
* C2 critic-evidence quality gate.
* C3 promotion gate (≥10 confirmations AND succeeded_after_count > 0)
  and the 7-day confirmations decay curve.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from config.defaults import default_config
from state.knowledge import (
    KnowledgeEntry,
    KnowledgeStore,
    TournamentEvent,
    _critic_evidence_quality,
    _evaluate_quarantine,
)
from state.paths import knowledge_path


def _hive_isolated_cfg() -> tuple:
    cfg = default_config()
    cfg.hive.enabled = False
    return cfg


# ---------------------------------------------------------------------------
# C1 — Quarantine
# ---------------------------------------------------------------------------


def test_evaluate_quarantine_trips_when_applied_above_floor_and_zero_success() -> None:
    entry = KnowledgeEntry(
        id="x",
        timestamp="2025-01-01T00:00:00+00:00",
        role_source="developer",
        tier="swarm",
        text="t",
        applied_count=11,
        succeeded_after_count=0,
    )
    should_q, reason = _evaluate_quarantine(entry)
    assert should_q is True
    assert reason == "applied_threshold_no_success"


def test_evaluate_quarantine_does_not_trip_below_floor() -> None:
    entry = KnowledgeEntry(
        id="x",
        timestamp="2025-01-01T00:00:00+00:00",
        role_source="developer",
        tier="swarm",
        text="t",
        applied_count=10,  # exactly at floor — must NOT trip
        succeeded_after_count=0,
    )
    should_q, reason = _evaluate_quarantine(entry)
    assert should_q is False and reason is None


def test_evaluate_quarantine_does_not_trip_above_success_ratio() -> None:
    entry = KnowledgeEntry(
        id="x",
        timestamp="2025-01-01T00:00:00+00:00",
        role_source="developer",
        tier="swarm",
        text="t",
        applied_count=100,
        succeeded_after_count=15,  # ratio 0.15 > 0.10 ceiling
    )
    should_q, _ = _evaluate_quarantine(entry)
    assert should_q is False


@pytest.mark.asyncio
async def test_quarantine_triggers_at_threshold(tmp_path: Path) -> None:
    """Eleventh injection of a zero-success entry flips quarantine."""
    cfg = _hive_isolated_cfg()
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=tmp_path / "hive.jsonl")

    entry = await store.record(
        "aaaa bbbb cccc dddd eeee ffff gggg hhhh iiii jjjj",
        role_source="developer",
        confidence=0.5,
    )
    assert entry is not None

    # Bump applied_count to 10 manually via successive inject_block calls.
    for _ in range(10):
        await store.inject_block("developer")

    # After the 10th call, the count is exactly 10 and ratio rule
    # does not engage (`applied_count > 10` is strict).
    entries = await store.read_all(tier="swarm")
    assert entries[0].applied_count == 10
    assert entries[0].quarantined is False

    # The 11th call brings count to 11 and trips quarantine.
    await store.inject_block("developer")
    entries = await store.read_all(tier="swarm")
    assert entries[0].applied_count == 11
    assert entries[0].quarantined is True


@pytest.mark.asyncio
async def test_quarantine_persists_to_audit_jsonl(tmp_path: Path) -> None:
    cfg = _hive_isolated_cfg()
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=tmp_path / "hive.jsonl")
    await store.record(
        "aaaa bbbb cccc dddd eeee ffff gggg hhhh iiii jjjj",
        role_source="developer",
        confidence=0.5,
    )
    for _ in range(11):
        await store.inject_block("developer")

    audit_path = knowledge_path(tmp_path).parent / "quarantine_audit.jsonl"
    assert audit_path.exists()
    lines = [ln for ln in audit_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    obj = json.loads(lines[0])
    # Sorted-key serialization guarantees stable field set.
    assert set(obj.keys()) == {
        "applied_count",
        "decided_at",
        "entry_id",
        "ratio",
        "reason",
        "succeeded_after_count",
    }
    assert obj["applied_count"] == 11
    assert obj["succeeded_after_count"] == 0
    assert obj["reason"] == "applied_threshold_no_success"
    assert obj["ratio"] == 0.0


@pytest.mark.asyncio
async def test_quarantined_entries_skipped_by_inject_block(tmp_path: Path) -> None:
    cfg = _hive_isolated_cfg()
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=tmp_path / "hive.jsonl")
    await store.record(
        "aaaa bbbb cccc dddd eeee ffff gggg hhhh iiii jjjj",
        role_source="developer",
        confidence=0.5,
    )
    # Trip the quarantine.
    for _ in range(11):
        await store.inject_block("developer")

    pre = (await store.read_all(tier="swarm"))[0]
    assert pre.quarantined is True
    assert pre.applied_count == 11

    # Subsequent inject_block calls must return empty (entry is the
    # only one in the store) and must NOT bump applied_count.
    block = await store.inject_block("developer")
    assert block == ""
    post = (await store.read_all(tier="swarm"))[0]
    assert post.applied_count == 11  # unchanged


@pytest.mark.asyncio
async def test_legacy_entry_without_quarantined_field_deserializes_false(
    tmp_path: Path,
) -> None:
    """A JSONL line written by an older release (no quarantined field)
    must deserialize as ``quarantined=False, last_applied_at=None``."""
    cfg = _hive_isolated_cfg()
    swarm_file = knowledge_path(tmp_path)
    swarm_file.parent.mkdir(parents=True, exist_ok=True)
    legacy = {
        "id": "legacy-1",
        "timestamp": "2024-01-01T00:00:00+00:00",
        "role_source": "developer",
        "tier": "swarm",
        "text": "legacy lesson",
        "confidence": 0.6,
        "applied_count": 5,
        "succeeded_after_count": 0,
        "confirmations": 1,
        "metadata": {},
    }
    swarm_file.write_text(json.dumps(legacy) + "\n")
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=tmp_path / "hive.jsonl")
    entries = await store.read_all(tier="swarm")
    assert len(entries) == 1
    assert entries[0].quarantined is False
    assert entries[0].last_applied_at is None


@pytest.mark.asyncio
async def test_succeeded_after_count_incremented_on_credit(tmp_path: Path) -> None:
    """credit_task_success bumps succeeded_after_count by 1."""
    cfg = _hive_isolated_cfg()
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=tmp_path / "hive.jsonl")
    e = await store.record(
        "uuuu vvvv wwww xxxx yyyy zzzz",
        role_source="developer",
        confidence=0.5,
    )
    assert e is not None
    n = await store.credit_task_success([e.id], task_id="t1", role="developer")
    assert n == 1
    post = (await store.read_all(tier="swarm"))[0]
    assert post.succeeded_after_count == 1


# ---------------------------------------------------------------------------
# C2 — Critic-evidence gate
# ---------------------------------------------------------------------------


def test_critic_evidence_gate_rejects_thin_evidence() -> None:
    """Explicit critic-abstention marker is rejected as ``thin``."""
    assert (
        _critic_evidence_quality(
            "The evidence provided is critically thin: no diff lines."
        )
        == "thin"
    )


def test_critic_evidence_gate_rejects_empty_body() -> None:
    """Empty / whitespace-only evidence rejects as ``thin``."""
    assert _critic_evidence_quality("") == "thin"
    assert _critic_evidence_quality("   \n\t ") == "thin"


def test_critic_evidence_gate_accepts_substantive_evidence() -> None:
    body = (
        "Reviewed three failing assertions in module foo. The pattern "
        "shows that the test fixture is missing the third parameter, "
        "leading to a None passed where a list was expected. Tests fail "
        "at line 42, 51, 60. Recommend updating the fixture in conftest."
    )
    assert _critic_evidence_quality(body) == "ok"


def test_critic_evidence_gate_accepts_short_structured_forensic_body() -> None:
    """v0.35.0 deviation: short structured forensic bodies are NOT thin.

    Tournament emitters write evidence like
    ``spec_hash=ab12 branch=1 of=3 error=adapter-busted``; these
    sub-80-char bodies are not critic abstentions and must pass.
    """
    body = "spec_hash=ab12 branch=1 of=3 error=adapter-busted"
    assert _critic_evidence_quality(body) == "ok"


def test_critic_evidence_gate_classifies_noise_pattern() -> None:
    # Contains a noise marker AND is < 200 chars → noise.
    body = (
        "coder adapter failure: subprocess returned exit code 1 after roughly "
        "five seconds and stderr was empty so we have nothing"
    )
    assert _critic_evidence_quality(body) == "noise"


@pytest.mark.asyncio
async def test_critic_evidence_gate_logs_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _hive_isolated_cfg()
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=tmp_path / "hive.jsonl")

    captured: list[tuple[str, dict]] = []

    async def fake_emit(op: str, payload: dict) -> None:
        captured.append((op, payload))

    monkeypatch.setattr(store, "_emit_ledger", fake_emit)

    event = TournamentEvent(
        event_type="discard",
        family="execute-phase",
        hypothesis="something went wrong",
        evidence="The evidence provided is critically thin: nothing useful.",
    )
    result = await store.record_tournament_event(event)
    assert result is None
    assert any(op == "critic_evidence_rejected" for op, _ in captured)
    # And the swarm must have no entries (gate ran before record).
    assert await store.read_all(tier="swarm") == []


# ---------------------------------------------------------------------------
# C3 — Promotion threshold + 7-day decay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promotion_requires_min_10_confirmations_and_success(
    tmp_path: Path,
) -> None:
    cfg = default_config()
    cfg.hive.enabled = True
    cfg.hive.path = tmp_path / "hive.jsonl"
    cfg.knowledge.promotion_min_confirmations = 10
    cfg.knowledge.promotion_min_confidence = 0.0
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=tmp_path / "hive.jsonl")

    # Entry with confirmations=10, success=0 must NOT promote.
    bad = KnowledgeEntry(
        id="e1",
        timestamp="2025-01-01T00:00:00+00:00",
        role_source="developer",
        tier="swarm",
        text="bad",
        confidence=0.9,
        confirmations=10,
        succeeded_after_count=0,
    )
    promoted = await store._promote_if_qualified(bad)
    assert promoted is False

    # Entry with confirmations=10, success=1 promotes.
    good = KnowledgeEntry(
        id="e2",
        timestamp="2025-01-01T00:00:00+00:00",
        role_source="developer",
        tier="swarm",
        text="good lesson with substantive content",
        confidence=0.9,
        confirmations=10,
        succeeded_after_count=1,
    )
    promoted = await store._promote_if_qualified(good)
    assert promoted is True


@pytest.mark.asyncio
async def test_promotion_rejected_below_min_confirmations(tmp_path: Path) -> None:
    cfg = default_config()
    cfg.hive.enabled = True
    cfg.hive.path = tmp_path / "hive.jsonl"
    cfg.knowledge.promotion_min_confirmations = 10
    cfg.knowledge.promotion_min_confidence = 0.0
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=tmp_path / "hive.jsonl")

    e = KnowledgeEntry(
        id="e3",
        timestamp="2025-01-01T00:00:00+00:00",
        role_source="developer",
        tier="swarm",
        text="ok",
        confidence=0.9,
        confirmations=9,  # one short
        succeeded_after_count=5,
    )
    promoted = await store._promote_if_qualified(e)
    assert promoted is False


def test_confirmations_decay_after_7_days(tmp_path: Path) -> None:
    """An entry whose last_applied_at is 8 days old must rank below a fresh peer."""
    cfg = _hive_isolated_cfg()
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=tmp_path / "hive.jsonl")

    now = datetime.now(timezone.utc)
    fresh = KnowledgeEntry(
        id="fresh",
        timestamp="2025-01-01T00:00:00+00:00",
        role_source="developer",
        tier="swarm",
        text="fresh",
        confidence=0.5,
        applied_count=5,
        last_applied_at=now,
    )
    stale = KnowledgeEntry(
        id="stale",
        timestamp="2025-01-01T00:00:00+00:00",
        role_source="developer",
        tier="swarm",
        text="stale",
        confidence=0.5,
        applied_count=5,
        last_applied_at=now - timedelta(days=8),
    )

    fresh_rank = store._rank_with_ts(fresh, now.timestamp())
    stale_rank = store._rank_with_ts(stale, now.timestamp())
    assert fresh_rank > stale_rank
