"""Tests for v0.10.0 per-pass adaptive semaphore ratcheting.

The :class:`tournament.core.Tournament` class exposes
``maybe_resize_semaphore(observed_rss_mb)`` which ratchets the in-flight
subprocess concurrency cap DOWN when observed memory pressure exceeds
``EXPECTED_RSS_MB`` × 1.3. Ratchet-up is intentionally not supported —
once slots are released they stay released for the rest of the
tournament's lifetime (mitigates oscillation; see plan rollback notes).

These tests construct a :class:`Tournament` with a stub LLM client and
exercise ``maybe_resize_semaphore`` in isolation. No actual judging
happens.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tournament.core import Tournament, TournamentConfig
from tournament.llm import StubLLMClient


# ---------------------------------------------------------------------------
# Helpers — minimal handler + artifact dir
# ---------------------------------------------------------------------------


class _StrHandler:
    """Tiny ContentHandler[str] for these tests — only the methods that
    Tournament's __init__ touches need to be present."""

    def render_for_critic(self, t: str, task_prompt: str) -> str:  # pragma: no cover
        return t

    def render_for_architect_b(
        self, task_prompt: str, a: str, critic_text: str
    ) -> str:  # pragma: no cover
        return a

    def render_for_synthesizer(
        self, task_prompt: str, x: str, y: str
    ) -> str:  # pragma: no cover
        return x

    def render_for_judge(
        self,
        task_prompt: str,
        v_a: str,
        v_b: str,
        v_ab: str,
        order_map: dict[int, str],
    ) -> str:  # pragma: no cover
        return v_a

    def parse_revision(self, revision_text: str, original: str) -> str:  # pragma: no cover
        return revision_text

    def parse_synthesis(
        self, synth_text: str, a: str, b: str
    ) -> str:  # pragma: no cover
        return synth_text

    def render_as_markdown(self, t: str) -> str:  # pragma: no cover
        return t

    def hash(self, t: str) -> str:  # pragma: no cover
        return str(hash(t))


def _make_tournament(tmp_path: Path, *, max_parallel: int = 4) -> Tournament[Any]:
    cfg = TournamentConfig(
        num_judges=3,
        convergence_k=1,
        max_rounds=1,
        max_parallel_subprocesses=max_parallel,
    )
    client = StubLLMClient(responses={})
    return Tournament(
        handler=_StrHandler(),
        client=client,
        cfg=cfg,
        artifact_dir=tmp_path,
    )


def _sem_capacity(sem: asyncio.Semaphore) -> int:
    """Read the internal slot count of an asyncio.Semaphore. ``_value`` is
    a CPython implementation detail but the same hatch the maybe_resize
    code reads, so it's appropriate for testing."""
    return sem._value  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Ratchet-down on high RSS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_resize_ratchets_down_on_high_rss(tmp_path: Path) -> None:
    """An RSS reading > 1.3 × EXPECTED_RSS_MB triggers a single ratchet
    step (current → max(1, current - 1))."""
    from tournament.core import EXPECTED_RSS_MB

    t = _make_tournament(tmp_path, max_parallel=4)
    assert _sem_capacity(t._sem) == 4

    # 2× over the budget — well above the 1.3× threshold.
    await t.maybe_resize_semaphore(EXPECTED_RSS_MB * 2)
    assert _sem_capacity(t._sem) == 3


# ---------------------------------------------------------------------------
# No ratchet-up on low RSS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_resize_does_not_ratchet_up_on_low_rss(tmp_path: Path) -> None:
    """RSS comfortably under the budget is a no-op. Ratchet-up is
    deliberately unsupported — once slots are released they never come
    back mid-tournament (see v0.10.0 plan: 'RATCHET DOWN ONLY')."""
    from tournament.core import EXPECTED_RSS_MB

    t = _make_tournament(tmp_path, max_parallel=4)
    assert _sem_capacity(t._sem) == 4

    # Half the budget — well under 1.3 ×.
    await t.maybe_resize_semaphore(EXPECTED_RSS_MB * 0.5)
    assert _sem_capacity(t._sem) == 4


# ---------------------------------------------------------------------------
# Ratchet floor at 1
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_resize_floor_at_1(tmp_path: Path) -> None:
    """Ratcheting never drops below 1 (one in-flight call is the
    minimum viable parallelism — zero would deadlock the engine)."""
    from tournament.core import EXPECTED_RSS_MB

    t = _make_tournament(tmp_path, max_parallel=2)
    # First trip: 2 → 1.
    await t.maybe_resize_semaphore(EXPECTED_RSS_MB * 2)
    assert _sem_capacity(t._sem) == 1
    # Second trip: 1 → would be 0 but clamped at 1 (no-op).
    await t.maybe_resize_semaphore(EXPECTED_RSS_MB * 2)
    assert _sem_capacity(t._sem) == 1


# ---------------------------------------------------------------------------
# Repeated ratchets persist across calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resize_persists_across_passes(tmp_path: Path) -> None:
    """Each high-RSS observation ratchets one step. Three high-RSS
    observations from cap=4 → cap=1 (4 → 3 → 2 → 1)."""
    from tournament.core import EXPECTED_RSS_MB

    t = _make_tournament(tmp_path, max_parallel=4)
    for _ in range(3):
        await t.maybe_resize_semaphore(EXPECTED_RSS_MB * 2)
    assert _sem_capacity(t._sem) == 1


# ---------------------------------------------------------------------------
# None observation is a no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_resize_handles_none(tmp_path: Path) -> None:
    """``observed_rss_mb=None`` (no PIDs reachable) is a no-op."""
    t = _make_tournament(tmp_path, max_parallel=4)
    await t.maybe_resize_semaphore(None)
    assert _sem_capacity(t._sem) == 4


# ---------------------------------------------------------------------------
# Boundary: exactly at the threshold does not ratchet
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_resize_at_threshold_no_ratchet(tmp_path: Path) -> None:
    """Observing exactly 1.3 × EXPECTED_RSS_MB is the inclusive upper bound
    of the 'within budget' band — no ratchet."""
    from tournament.core import EXPECTED_RSS_MB

    t = _make_tournament(tmp_path, max_parallel=4)
    await t.maybe_resize_semaphore(EXPECTED_RSS_MB * 1.3)
    assert _sem_capacity(t._sem) == 4
