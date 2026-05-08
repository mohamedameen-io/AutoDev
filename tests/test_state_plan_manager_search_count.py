"""v0.17.0 S2: ``PlanManager.increment_search`` mirrors increment_pivot/discard."""

from __future__ import annotations

from pathlib import Path

import pytest

from state.plan_manager import PlanManager


@pytest.mark.asyncio
async def test_increment_search_creates_state_when_missing(
    tmp_path: Path,
) -> None:
    pm = PlanManager(tmp_path, session_id="t")
    state = await pm.increment_search("task-1")
    assert state.search_count == 1
    assert state.last_event == "web_search"


@pytest.mark.asyncio
async def test_increment_search_bumps_existing_state(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="t")
    await pm.increment_pivot("task-1")  # pivot=1
    await pm.increment_search("task-1")  # search=1
    state = await pm.increment_search("task-1")  # search=2
    assert state.search_count == 2
    assert state.pivot_count == 1


@pytest.mark.asyncio
async def test_increment_search_preserves_other_counters(
    tmp_path: Path,
) -> None:
    pm = PlanManager(tmp_path, session_id="t")
    await pm.increment_discard("task-1")
    await pm.increment_discard("task-1")
    await pm.increment_pivot("task-1")
    state = await pm.increment_search("task-1")
    assert state.discard_count == 2
    assert state.pivot_count == 1
    assert state.search_count == 1


@pytest.mark.asyncio
async def test_get_stuck_state_returns_search_count(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="t")
    await pm.increment_search("task-x")
    await pm.increment_search("task-x")
    state = await pm.get_stuck_state("task-x")
    assert state.search_count == 2


@pytest.mark.asyncio
async def test_reset_stuck_state_zeroes_search_count(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="t")
    await pm.increment_search("task-x")
    await pm.reset_stuck_state("task-x")
    state = await pm.get_stuck_state("task-x")
    assert state.search_count == 0
