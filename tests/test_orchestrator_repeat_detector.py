"""v0.17.0 S4: ``RepeatedHypothesisDetector`` advisory check.

Walks ``discard`` :class:`TournamentEvent` lessons from the past 14 days
and computes ``jaccard_bigrams`` similarity between the candidate
hypothesis and each prior one. Returns True iff any past discard exceeds
``threshold`` (default 0.6 — same as the dedup-threshold used elsewhere
in :mod:`state.knowledge`).

Advisory only — the multi-branch dispatcher tags branches with
``metadata={"hypothesis_repeat": True}`` and logs a warning, but does
NOT block execution. Re-attempting a discarded approach is sometimes
the right thing (the prior failure may have been transient).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config.defaults import default_config
from state.knowledge import KnowledgeStore, TournamentEvent


@pytest.fixture
def knowledge_store(tmp_path: Path) -> KnowledgeStore:
    """Build a KnowledgeStore over a tmp dir."""
    cfg = default_config()
    return KnowledgeStore(tmp_path, cfg=cfg)


@pytest.mark.asyncio
async def test_no_priors_returns_false(knowledge_store: KnowledgeStore) -> None:
    """Empty history: nothing to repeat."""
    from orchestrator.repeat_detector import RepeatedHypothesisDetector

    detector = RepeatedHypothesisDetector(knowledge_store)
    result = await detector.is_repeat("any new hypothesis")
    assert result is False


@pytest.mark.asyncio
async def test_recent_similar_discard_returns_true(
    knowledge_store: KnowledgeStore,
) -> None:
    """A recent discard with high-similarity hypothesis triggers repeat."""
    from orchestrator.repeat_detector import RepeatedHypothesisDetector

    await knowledge_store.record_tournament_event(
        TournamentEvent(
            event_type="discard",
            family="plan-tournament",
            hypothesis="rewrite the migration script using bulk inserts",
            evidence="2 judges flagged regressions",
            rollback_reason="too aggressive",
        )
    )
    detector = RepeatedHypothesisDetector(knowledge_store)
    # Near-identical text — bigram Jaccard well above 0.6.
    result = await detector.is_repeat(
        "rewrite the migration script using bulk inserts"
    )
    assert result is True


@pytest.mark.asyncio
async def test_dissimilar_hypothesis_returns_false(
    knowledge_store: KnowledgeStore,
) -> None:
    from orchestrator.repeat_detector import RepeatedHypothesisDetector

    await knowledge_store.record_tournament_event(
        TournamentEvent(
            event_type="discard",
            family="plan-tournament",
            hypothesis="rewrite the migration script using bulk inserts",
            evidence="x",
        )
    )
    detector = RepeatedHypothesisDetector(knowledge_store)
    result = await detector.is_repeat(
        "add caching to the auth middleware"
    )
    assert result is False


@pytest.mark.asyncio
async def test_winner_promoted_event_does_not_trigger(
    knowledge_store: KnowledgeStore,
) -> None:
    """Only ``discard`` events count — past winners are not repeats."""
    from orchestrator.repeat_detector import RepeatedHypothesisDetector

    await knowledge_store.record_tournament_event(
        TournamentEvent(
            event_type="winner_promoted",
            family="plan-tournament",
            hypothesis="rewrite the migration script using bulk inserts",
            evidence="x",
        )
    )
    detector = RepeatedHypothesisDetector(knowledge_store)
    result = await detector.is_repeat(
        "rewrite the migration script using bulk inserts"
    )
    assert result is False


@pytest.mark.asyncio
async def test_family_filter_narrows_search(
    knowledge_store: KnowledgeStore,
) -> None:
    """When ``family`` is set, only events with matching family count."""
    from orchestrator.repeat_detector import RepeatedHypothesisDetector

    await knowledge_store.record_tournament_event(
        TournamentEvent(
            event_type="discard",
            family="plan-tournament",
            hypothesis="rewrite the migration script using bulk inserts",
            evidence="x",
        )
    )
    detector = RepeatedHypothesisDetector(knowledge_store)
    # Same hypothesis, but filter on a different family — no match.
    result = await detector.is_repeat(
        "rewrite the migration script using bulk inserts",
        family="impl-tournament",
    )
    assert result is False
    # And matching family DOES trigger.
    result = await detector.is_repeat(
        "rewrite the migration script using bulk inserts",
        family="plan-tournament",
    )
    assert result is True


@pytest.mark.asyncio
async def test_threshold_is_tunable(
    knowledge_store: KnowledgeStore,
) -> None:
    """Higher threshold filters out moderate similarity."""
    from orchestrator.repeat_detector import RepeatedHypothesisDetector

    await knowledge_store.record_tournament_event(
        TournamentEvent(
            event_type="discard",
            family="x",
            hypothesis="rewrite migration with bulk inserts",
            evidence="x",
        )
    )
    detector = RepeatedHypothesisDetector(knowledge_store)
    # Loose threshold: similar enough.
    assert await detector.is_repeat(
        "rewrite migration with batch inserts", threshold=0.4
    ) is True
    # Strict threshold: not similar enough.
    assert await detector.is_repeat(
        "rewrite migration with batch inserts", threshold=0.99
    ) is False


@pytest.mark.asyncio
async def test_old_events_excluded_by_default(
    knowledge_store: KnowledgeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discards older than 14 days don't count as repeats."""
    from datetime import datetime, timedelta, timezone

    from orchestrator.repeat_detector import RepeatedHypothesisDetector

    # Persist a discard, then mutate its timestamp into the past.
    entry = await knowledge_store.record_tournament_event(
        TournamentEvent(
            event_type="discard",
            family="x",
            hypothesis="ancient hypothesis",
            evidence="x",
        )
    )
    assert entry is not None
    # Re-write the swarm file with the timestamp set to 30d ago.
    from state.paths import knowledge_path
    import json

    swarm = knowledge_path(knowledge_store._cwd)
    raw_lines = swarm.read_text(encoding="utf-8").splitlines()
    objs = [json.loads(ln) for ln in raw_lines if ln.strip()]
    old_ts = (
        datetime.now(timezone.utc) - timedelta(days=30)
    ).isoformat()
    for obj in objs:
        obj["timestamp"] = old_ts
    swarm.write_text(
        "\n".join(json.dumps(o) for o in objs) + "\n", encoding="utf-8"
    )

    detector = RepeatedHypothesisDetector(knowledge_store)
    result = await detector.is_repeat("ancient hypothesis")
    assert result is False
