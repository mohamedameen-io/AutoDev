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


# ---------------------------------------------------------------------------
# _run_judges integration: PIDs collected and probe runs at pass-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_judges_collects_pids_and_probes_rss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_run_judges`` clears ``_pass_judge_pids`` at start, ``_guarded_judge``
    appends to it during the pass, and at end calls
    ``measure_subprocess_rss`` → ``maybe_resize_semaphore``."""
    from tournament import core as core_mod

    t = _make_tournament(tmp_path, max_parallel=4)

    # Force ``_guarded_judge`` to fake completion + register a PID.
    async def fake_guarded(self, user, model, pass_num, judge_index, order):
        self._pass_judge_pids.append(1000 + judge_index)
        return "RANKING: 1, 2, 3"

    monkeypatch.setattr(core_mod.Tournament, "_guarded_judge", fake_guarded)

    # Mock the probe to return a known RSS.
    measure_calls: list[list[int]] = []

    def fake_measure(pids):
        measure_calls.append(list(pids))
        return 1500.0  # well above 1.3 × 1024 → triggers ratchet

    monkeypatch.setattr(core_mod, "measure_subprocess_rss", fake_measure)

    await t._run_judges(
        task_prompt="prompt",
        v_a="A",
        v_b="B",
        v_ab="AB",
        model=None,
        pass_num=1,
    )

    assert len(measure_calls) == 1
    # 3 judges → PIDs 1000, 1001, 1002 collected.
    assert sorted(measure_calls[0]) == [1000, 1001, 1002]
    # Ratchet fired: 4 → 3.
    assert _sem_capacity(t._sem) == 3


@pytest.mark.asyncio
async def test_run_judges_probe_failure_does_not_break_tournament(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flaky probe (raises) is logged and swallowed — no tournament impact."""
    from tournament import core as core_mod

    t = _make_tournament(tmp_path, max_parallel=4)

    async def fake_guarded(self, user, model, pass_num, judge_index, order):
        return "RANKING: 1, 2, 3"

    monkeypatch.setattr(core_mod.Tournament, "_guarded_judge", fake_guarded)

    def boom(pids):
        raise RuntimeError("psutil exploded")

    monkeypatch.setattr(core_mod, "measure_subprocess_rss", boom)

    # Should NOT raise.
    await t._run_judges(
        task_prompt="prompt",
        v_a="A",
        v_b="B",
        v_ab="AB",
        model=None,
        pass_num=1,
    )
    # Semaphore unchanged — no ratchet on probe failure.
    assert _sem_capacity(t._sem) == 4


@pytest.mark.asyncio
async def test_run_judges_clears_pid_buffer_between_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each pass starts with an empty ``_pass_judge_pids`` so a previous
    pass's PIDs do not contaminate the new pass's measurement."""
    from tournament import core as core_mod

    t = _make_tournament(tmp_path, max_parallel=4)

    pass_call: list[int] = [0]

    async def fake_guarded(self, user, model, pass_num, judge_index, order):
        # Each pass uses different PIDs.
        self._pass_judge_pids.append(pass_num * 100 + judge_index)
        return "RANKING: 1, 2, 3"

    monkeypatch.setattr(core_mod.Tournament, "_guarded_judge", fake_guarded)

    measured: list[list[int]] = []

    def fake_measure(pids):
        measured.append(list(pids))
        pass_call[0] += 1
        return None  # No ratchet.

    monkeypatch.setattr(core_mod, "measure_subprocess_rss", fake_measure)

    await t._run_judges("p", "A", "B", "AB", None, pass_num=1)
    await t._run_judges("p", "A", "B", "AB", None, pass_num=2)

    assert len(measured) == 2
    # Pass 1: PIDs 100, 101, 102.
    assert sorted(measured[0]) == [100, 101, 102]
    # Pass 2: PIDs 200, 201, 202 — NOT 100..102 mixed in.
    assert sorted(measured[1]) == [200, 201, 202]
