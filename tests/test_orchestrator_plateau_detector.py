"""v0.18.0 B2: tests for the plateau detector."""

from __future__ import annotations

from pathlib import Path

import pytest

from config.defaults import default_config
from config.schema import BranchConfig
from orchestrator.plateau_detector import PlateauDetector
from state.knowledge import KnowledgeStore, TournamentEvent


def _store(tmp_path: Path) -> KnowledgeStore:
    return KnowledgeStore(cwd=tmp_path, cfg=default_config())


@pytest.mark.asyncio
async def test_detect_plateau_below_3_events_returns_false(tmp_path: Path) -> None:
    """Fewer than 3 events for the family → no plateau."""
    store = _store(tmp_path)
    pd = PlateauDetector(store)
    await store.record_tournament_event(TournamentEvent(
        event_type="discard", family="plan-tournament",
        hypothesis="zzz unique abc xyz lesson 12345",
        evidence="distant evidence aaa bbb 9876",
    ))
    assert await pd.detect_plateau("plan-tournament", window=4) is False


@pytest.mark.asyncio
async def test_detect_plateau_no_winner_in_window_returns_true(
    tmp_path: Path,
) -> None:
    """3+ events, all non-winner → plateau."""
    store = _store(tmp_path)
    pd = PlateauDetector(store)
    # Disable dedup merging to ensure all 4 events are recorded as
    # separate entries — this is a unit test for the detector logic.
    store.knowledge_config.dedup_threshold = 1.01

    distinct_words = [
        "alpha beta gamma delta epsilon zeta eta theta iota kappa",
        "kilo lima mike november oscar papa quebec romeo sigma tango",
        "uniform victor whiskey xray yankee zulu omega phi chi psi",
        "qatar stockholm rio paris madrid moscow lima oslo seoul cairo",
    ]
    for i in range(4):
        await store.record_tournament_event(TournamentEvent(
            event_type="discard", family="plan-tournament",
            hypothesis=f"discard hypothesis {i}: {distinct_words[i]}",
            evidence=f"evidence {i}: {distinct_words[i]} forensic data extra",
        ))
    assert await pd.detect_plateau("plan-tournament", window=4) is True


@pytest.mark.asyncio
async def test_detect_plateau_winner_in_window_returns_false(tmp_path: Path) -> None:
    """A winner_promoted event in the window → no plateau."""
    store = _store(tmp_path)
    pd = PlateauDetector(store)
    store.knowledge_config.dedup_threshold = 1.01

    for i in range(2):
        await store.record_tournament_event(TournamentEvent(
            event_type="discard", family="plan-tournament",
            hypothesis=f"early discard {i} unique structure code path",
            evidence=f"early failure {i} forensic detail trace info",
        ))
    await store.record_tournament_event(TournamentEvent(
        event_type="winner_promoted", family="plan-tournament",
        hypothesis="middle winner attempt structure code path zebra",
        evidence="middle success detail stack info forensic zebra",
    ))
    for i in range(2):
        await store.record_tournament_event(TournamentEvent(
            event_type="discard", family="plan-tournament",
            hypothesis=f"late discard {i} unique alternate strategy beta",
            evidence=f"late failure {i} forensic alternate detail beta",
        ))
    assert await pd.detect_plateau("plan-tournament", window=4) is False


@pytest.mark.asyncio
async def test_detect_cross_family_plateau(tmp_path: Path) -> None:
    """Cross-family plateau fires when no winner_promoted events anywhere."""
    store = _store(tmp_path)
    pd = PlateauDetector(store)
    # Disable dedup so the 5 events are all stored separately.
    store.knowledge_config.dedup_threshold = 1.01

    for i in range(5):
        await store.record_tournament_event(TournamentEvent(
            event_type="discard", family=f"family-{i}",
            hypothesis=f"branch attempt {i} unique structure code path",
            evidence=f"failure detail {i} stack trace excerpt forensic info",
        ))
    assert await pd.detect_cross_family_plateau(window=5) is True


@pytest.mark.asyncio
async def test_detect_cross_family_no_plateau_with_winner(tmp_path: Path) -> None:
    """Cross-family plateau does NOT fire when a winner_promoted exists."""
    store = _store(tmp_path)
    pd = PlateauDetector(store)
    store.knowledge_config.dedup_threshold = 1.01

    await store.record_tournament_event(TournamentEvent(
        event_type="winner_promoted", family="family-x",
        hypothesis="winner attempt structure code path zebra",
        evidence="success detail stack info forensic zebra",
    ))
    for i in range(3):
        await store.record_tournament_event(TournamentEvent(
            event_type="discard", family="family-y",
            hypothesis=f"branch attempt {i} unique structure code path",
            evidence=f"failure detail {i} stack trace excerpt forensic info",
        ))
    assert await pd.detect_cross_family_plateau(window=5) is False


@pytest.mark.asyncio
async def test_force_distant_scout_picks_matching_family(tmp_path: Path) -> None:
    """force_distant_scout mutates the branch with the plateaued family."""
    store = _store(tmp_path)
    pd = PlateauDetector(store)
    branches = [
        BranchConfig(lane="local-tweak", family="alpha"),
        BranchConfig(lane="architectural", family="beta"),
        BranchConfig(lane="local-tweak", family="gamma"),
    ]
    new_branches = await pd.force_distant_scout(branches, plateaued_family="beta")
    assert new_branches[1].lane == "distant-scout"
    # Other branches unchanged.
    assert new_branches[0].lane == "local-tweak"
    assert new_branches[2].lane == "local-tweak"
    # Original list NOT mutated.
    assert branches[1].lane == "architectural"


@pytest.mark.asyncio
async def test_force_distant_scout_no_match_picks_first(tmp_path: Path) -> None:
    """No family match → mutate the first branch."""
    store = _store(tmp_path)
    pd = PlateauDetector(store)
    branches = [
        BranchConfig(lane="local-tweak", family="alpha"),
        BranchConfig(lane="architectural", family="beta"),
    ]
    new_branches = await pd.force_distant_scout(branches, plateaued_family="omega")
    assert new_branches[0].lane == "distant-scout"
    assert new_branches[1].lane == "architectural"


@pytest.mark.asyncio
async def test_force_distant_scout_empty_list(tmp_path: Path) -> None:
    """Empty list returns empty list."""
    store = _store(tmp_path)
    pd = PlateauDetector(store)
    assert await pd.force_distant_scout([]) == []
