"""v0.15.0: cross-run survival contract for the swarm-tier knowledge store.

The swarm-tier file at ``<cwd>/.autodev/knowledge.jsonl`` is per-project and
survives across separate ``autodev plan`` invocations because nothing in the
orchestrator's normal flow deletes it. This test makes the contract explicit:
a lesson written by one :class:`KnowledgeStore` instance must be visible to a
*fresh* instance constructed against the same ``cwd`` afterwards.

The "fresh process" semantics are simulated by re-instantiating the store
(the on-disk JSONL is the only state that needs to round-trip — there is no
in-memory cache that could leak the result between instances).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config.defaults import default_config
from state.knowledge import KnowledgeStore, TournamentEvent
from state.paths import knowledge_path


@pytest.mark.asyncio
async def test_swarm_tier_survives_across_runs(tmp_path: Path) -> None:
    """A lesson recorded by one store instance must be visible to a fresh
    one constructed against the same project ``cwd``.
    """
    cfg = default_config()
    cfg.hive.enabled = False

    # First "run": record a lesson via record_tournament_event.
    first = KnowledgeStore(tmp_path, cfg=cfg, hive_path=tmp_path / "hive.jsonl")
    event = TournamentEvent(
        event_type="winner_promoted",
        family="plan-tournament",
        hypothesis="narrow edit_scope produces faster convergence",
        evidence="branch 1 converged in 2 passes; branch 2 in 5",
        next_action_hint="prefer narrow edit_scope on Unity-class repos",
    )
    written = await first.record_tournament_event(event)
    assert written is not None

    # Verify on-disk artifact exists at the documented path.
    swarm_file = knowledge_path(tmp_path)
    assert swarm_file.exists()
    assert swarm_file.read_text(encoding="utf-8").strip() != ""

    # Simulate a fresh process: a new KnowledgeStore + clean cfg.
    fresh_cfg = default_config()
    fresh_cfg.hive.enabled = False
    fresh = KnowledgeStore(tmp_path, cfg=fresh_cfg, hive_path=tmp_path / "hive.jsonl")
    entries = await fresh.read_all(tier="swarm")
    assert len(entries) == 1
    assert entries[0].id == written.id
    assert "narrow edit_scope" in entries[0].text


@pytest.mark.asyncio
async def test_inject_block_returns_prior_run_content(tmp_path: Path) -> None:
    """A fresh ``inject_block(role="critic_t")`` must surface the lesson the
    previous run wrote via :meth:`record_tournament_event`. This is the
    end-to-end consumption path the per-pass critic relies on.
    """
    cfg = default_config()
    cfg.hive.enabled = False
    # Pin denylist + max_inject so the assertion is robust to default tweaks.
    cfg.knowledge.max_inject_count = 3
    cfg.knowledge.denylist_roles = ["judge"]

    first = KnowledgeStore(tmp_path, cfg=cfg, hive_path=tmp_path / "hive.jsonl")
    await first.record_tournament_event(
        TournamentEvent(
            event_type="discard",
            family="plan-tournament",
            hypothesis="phase 1 collapsed to 'investigate' with no concrete files",
            evidence="reviewer flagged scope leak",
            rollback_reason="phase too vague to execute",
        )
    )

    fresh_cfg = default_config()
    fresh_cfg.hive.enabled = False
    fresh_cfg.knowledge.max_inject_count = 3
    fresh_cfg.knowledge.denylist_roles = ["judge"]
    fresh = KnowledgeStore(tmp_path, cfg=fresh_cfg, hive_path=tmp_path / "hive.jsonl")
    block = await fresh.inject_block(role="critic_t")
    # Block format: ``Lessons learned from prior work:\n- [conf:0.50] ...``.
    assert "Lessons learned from prior work" in block
    assert "discard" in block
    assert "phase too vague" in block
