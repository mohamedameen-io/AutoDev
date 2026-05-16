"""v0.15.0: TournamentEvent + record_tournament_event helper on KnowledgeStore.

Validates that the tournament-event write path:
* Persists structured ASI-style text into the swarm tier.
* Sets confidence by event type (winner_promoted=0.85, discard=0.5,
  escalation=0.7, course_correction=0.6, soft_blocker=0.8).
* The swarm tier file at ``<cwd>/.autodev/knowledge.jsonl`` survives across
  re-instantiation of :class:`KnowledgeStore` — i.e. across runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config.defaults import default_config
from state.knowledge import KnowledgeStore, TournamentEvent
from state.paths import knowledge_path


@pytest.mark.asyncio
async def test_record_tournament_event_persists_to_swarm_tier(tmp_path: Path) -> None:
    cfg = default_config()
    cfg.hive.enabled = False
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=tmp_path / "hive.jsonl")
    event = TournamentEvent(
        event_type="winner_promoted",
        family="plan-tournament",
        hypothesis="opengles-only edit_scope keeps developer focused",
        evidence="branch-1-glesonly converged in 2 passes; competing branches diverged after producing materially wider edit scopes",
        rollback_reason=None,
        next_action_hint="prefer narrow edit_scope when refactoring inside Unity-class repos",
    )
    written = await store.record_tournament_event(event)
    assert written is not None
    entries = await store.read_all(tier="swarm")
    assert len(entries) == 1
    text = entries[0].text
    # Structured ASI-style content must mention the family + hypothesis.
    assert "plan-tournament" in text
    assert "opengles-only" in text


@pytest.mark.asyncio
async def test_record_tournament_event_winner_promoted_high_confidence(
    tmp_path: Path,
) -> None:
    cfg = default_config()
    cfg.hive.enabled = False
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=tmp_path / "hive.jsonl")
    event = TournamentEvent(
        event_type="winner_promoted",
        family="plan-tournament",
        hypothesis="hypothesis A",
        evidence="evidence body for winner with at least eighty characters total to clear the v0.35.0 C2 thin-evidence gate threshold",
    )
    written = await store.record_tournament_event(event)
    assert written is not None
    assert written.confidence == pytest.approx(0.85)


@pytest.mark.asyncio
async def test_record_tournament_event_discard_medium_confidence(tmp_path: Path) -> None:
    cfg = default_config()
    cfg.hive.enabled = False
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=tmp_path / "hive.jsonl")
    event = TournamentEvent(
        event_type="discard",
        family="plan-tournament",
        hypothesis="discarded candidate B widened scope unnecessarily",
        evidence="judges 1,3,4 demoted on growth-ratio cap; full deliberation log shows 7 distinct objections across the panel",
        rollback_reason="oversize-AB demotion",
    )
    written = await store.record_tournament_event(event)
    assert written is not None
    assert written.confidence == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_record_tournament_event_escalation_confidence(tmp_path: Path) -> None:
    cfg = default_config()
    cfg.hive.enabled = False
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=tmp_path / "hive.jsonl")
    event = TournamentEvent(
        event_type="escalation",
        family="execute-phase",
        hypothesis="task ABC repeatedly fails",
        evidence="3 retries exhausted with no progress; last attempt produced an identical diff to attempt one, confirming a stable loop",
    )
    written = await store.record_tournament_event(event)
    assert written is not None
    assert written.confidence == pytest.approx(0.7)


@pytest.mark.asyncio
async def test_record_tournament_event_course_correction_confidence(
    tmp_path: Path,
) -> None:
    cfg = default_config()
    cfg.hive.enabled = False
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=tmp_path / "hive.jsonl")
    event = TournamentEvent(
        event_type="course_correction",
        family="prm",
        hypothesis="repetition_loop pattern detected",
        evidence="3 identical (developer, edit, src/foo.py) calls within the past 90 seconds; trajectory store flagged the repetition pattern",
    )
    written = await store.record_tournament_event(event)
    assert written is not None
    assert written.confidence == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_record_tournament_event_soft_blocker_confidence(tmp_path: Path) -> None:
    cfg = default_config()
    cfg.hive.enabled = False
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=tmp_path / "hive.jsonl")
    event = TournamentEvent(
        event_type="soft_blocker",
        family="execute-phase",
        hypothesis="task X requires human decision on FFI ABI",
        evidence="3 pivots failed; critic flagged soft-blocker citing missing external decision; further work requires a human policy choice",
    )
    written = await store.record_tournament_event(event)
    assert written is not None
    assert written.confidence == pytest.approx(0.8)


@pytest.mark.asyncio
async def test_record_tournament_event_disabled_returns_none(tmp_path: Path) -> None:
    cfg = default_config()
    cfg.knowledge.enabled = False
    cfg.hive.enabled = False
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=tmp_path / "hive.jsonl")
    event = TournamentEvent(
        event_type="winner_promoted",
        family="plan-tournament",
        hypothesis="...",
        evidence="placeholder evidence body long enough to clear the v0.35.0 C2 thin-evidence gate threshold without saying anything load-bearing",
    )
    assert await store.record_tournament_event(event) is None


@pytest.mark.asyncio
async def test_record_tournament_event_writes_role_source_critic_t(
    tmp_path: Path,
) -> None:
    """Tournament events should be tagged role_source='critic_t' so they are
    surfaced to the per-pass critic via inject_block(role='critic_t')."""
    cfg = default_config()
    cfg.hive.enabled = False
    store = KnowledgeStore(tmp_path, cfg=cfg, hive_path=tmp_path / "hive.jsonl")
    event = TournamentEvent(
        event_type="discard",
        family="plan-tournament",
        hypothesis="X widened scope",
        evidence="placeholder evidence body long enough to clear the v0.35.0 C2 thin-evidence gate threshold without saying anything load-bearing",
    )
    written = await store.record_tournament_event(event)
    assert written is not None
    assert written.role_source == "critic_t"


def test_swarm_path_is_per_project(tmp_path: Path) -> None:
    """Sanity: the swarm-tier file path is ``<cwd>/.autodev/knowledge.jsonl``."""
    expected = tmp_path / ".autodev" / "knowledge.jsonl"
    assert knowledge_path(tmp_path) == expected
