"""Tests for the v0.6.2 oversize-AB demotion via ``max_plan_lines_growth_ratio``.

When the Borda winner is AB AND the synthesizer's output is more than
``ratio * len(incumbent_lines)`` lines, AB is demoted to second place. The
runner-up (between A and B by Borda score) becomes the effective winner.

Behaviour summary:
    - A>B in Borda → demote to A → incumbent unchanged → streak increments.
    - B>A in Borda → demote to B → incumbent updates → streak resets.
    - A==B in Borda → demote to A (conservative tiebreak).
    - ratio is None or AB within ratio → AB kept as winner (no-op).
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from tournament import (
    StubLLMClient,
    Tournament,
    TournamentConfig,
)
from tournament.core import _demote_oversized_winner
from tournament.plan_tournament import PlanContentHandler


# ── Unit tests for _demote_oversized_winner ───────────────────────────────


def test_oversized_ab_demoted_to_second_place_a_winner() -> None:
    """When AB is too long and A's Borda score exceeds B's, AB demotes to A."""
    incumbent_md = "\n".join("line" for _ in range(200))
    v_ab_md = "\n".join("line" for _ in range(400))  # 2.0×
    scores = {"A": 10, "B": 5, "AB": 15}

    new_winner, new_scores = _demote_oversized_winner(
        winner="AB",
        scores=scores,
        incumbent_md=incumbent_md,
        v_ab_md=v_ab_md,
        ratio=1.5,
    )
    assert new_winner == "A"
    # Scores dict is preserved so on-disk artifacts retain the actual Borda counts.
    assert new_scores == scores


def test_oversized_ab_demoted_to_b_when_b_higher() -> None:
    """When AB is too long and B's Borda score exceeds A's, AB demotes to B."""
    incumbent_md = "\n".join("line" for _ in range(200))
    v_ab_md = "\n".join("line" for _ in range(400))
    scores = {"A": 5, "B": 10, "AB": 15}

    new_winner, new_scores = _demote_oversized_winner(
        winner="AB",
        scores=scores,
        incumbent_md=incumbent_md,
        v_ab_md=v_ab_md,
        ratio=1.5,
    )
    assert new_winner == "B"
    assert new_scores == scores


def test_oversized_ab_demoted_to_a_on_score_tie() -> None:
    """When A and B Borda scores tie, demote to A (conservative — incumbent unchanged)."""
    incumbent_md = "\n".join("line" for _ in range(200))
    v_ab_md = "\n".join("line" for _ in range(400))
    scores = {"A": 7, "B": 7, "AB": 15}

    new_winner, _new_scores = _demote_oversized_winner(
        winner="AB",
        scores=scores,
        incumbent_md=incumbent_md,
        v_ab_md=v_ab_md,
        ratio=1.5,
    )
    assert new_winner == "A"


def test_undersized_ab_kept_as_winner() -> None:
    """If AB is within the growth ratio, keep AB — no demotion."""
    incumbent_md = "\n".join("line" for _ in range(200))
    v_ab_md = "\n".join("line" for _ in range(250))  # 1.25×
    scores = {"A": 5, "B": 10, "AB": 15}

    new_winner, new_scores = _demote_oversized_winner(
        winner="AB",
        scores=scores,
        incumbent_md=incumbent_md,
        v_ab_md=v_ab_md,
        ratio=1.5,
    )
    assert new_winner == "AB"
    assert new_scores == scores


def test_growth_ratio_none_disables_check() -> None:
    """``ratio=None`` disables the demotion check entirely (legacy default)."""
    incumbent_md = "\n".join("line" for _ in range(200))
    v_ab_md = "\n".join("line" for _ in range(2000))  # 10× — extreme growth
    scores = {"A": 5, "B": 10, "AB": 15}

    new_winner, new_scores = _demote_oversized_winner(
        winner="AB",
        scores=scores,
        incumbent_md=incumbent_md,
        v_ab_md=v_ab_md,
        ratio=None,
    )
    assert new_winner == "AB"
    assert new_scores == scores


def test_demotion_skipped_when_winner_is_not_ab() -> None:
    """If Borda picked A or B, no demotion logic applies (only AB can grow)."""
    incumbent_md = "\n".join("line" for _ in range(10))
    v_ab_md = "\n".join("line" for _ in range(1000))  # 100× — irrelevant
    scores = {"A": 15, "B": 10, "AB": 5}

    new_winner, _ = _demote_oversized_winner(
        winner="A",
        scores=scores,
        incumbent_md=incumbent_md,
        v_ab_md=v_ab_md,
        ratio=1.5,
    )
    assert new_winner == "A"


def test_demotion_handles_empty_incumbent_lines() -> None:
    """If incumbent has zero lines, the ratio check should still be safe.

    Edge case: an empty incumbent has 0 lines; any non-empty AB is technically
    'infinite ratio'. This is treated as oversize so AB demotes.
    """
    incumbent_md = ""  # 0 lines
    v_ab_md = "\n".join("line" for _ in range(50))
    scores = {"A": 10, "B": 5, "AB": 15}

    new_winner, _ = _demote_oversized_winner(
        winner="AB",
        scores=scores,
        incumbent_md=incumbent_md,
        v_ab_md=v_ab_md,
        ratio=1.5,
    )
    # With 0 incumbent lines, any non-empty AB triggers demotion.
    assert new_winner == "A"


# ── Integration test — full Tournament.run with oversize demotion ─────────


def _judge_prefer(prompt_text: str, prefer_marker: str) -> str:
    """Helper mirroring the one in test_tournament_runaway_repro.py."""
    offsets: dict[int, int] = {}
    for slot in (1, 2, 3):
        idx = prompt_text.find(f"PROPOSAL {slot}:")
        if idx >= 0:
            offsets[slot] = idx
    ordered = sorted(offsets.items(), key=lambda kv: kv[1])
    slot_end: dict[int, int] = {}
    for i, (slot, start) in enumerate(ordered):
        end = ordered[i + 1][1] if i + 1 < len(ordered) else len(prompt_text)
        slot_end[slot] = end
    preferred: int | None = None
    for slot, start in offsets.items():
        if prefer_marker in prompt_text[start : slot_end[slot]]:
            preferred = slot
            break
    assert preferred is not None
    others = [s for s in (1, 2, 3) if s != preferred]
    return f"RANKING: {preferred}, {others[0]}, {others[1]}"


@pytest.mark.asyncio
async def test_run_pass_demotes_oversized_ab_in_full_loop(tmp_path: Path) -> None:
    """End-to-end: when AB doubles incumbent length and ratio=1.5, AB is
    demoted, the demotion is recorded in pass meta, the persisted incumbent
    is the demoted variant (A here, since A>B by Borda), and ``runaway_trigger``
    is NOT set (this is a demotion, not a runaway).
    """
    incumbent_initial = "# Plan: foo\n" + "\n".join(f"## Phase {i}" for i in range(199))
    # 200 lines total in incumbent.
    assert len(incumbent_initial.splitlines()) == 200

    # Synthesizer emits a 400-line bloated AB body each pass.
    bloated_ab = (
        "# Plan: AB_BLOATED\n"
        + "\n".join(f"## Phase {i} restated" for i in range(399))
    )
    assert len(bloated_ab.splitlines()) == 400

    cfg = TournamentConfig(
        num_judges=1,
        convergence_k=2,
        max_rounds=2,
        max_plan_lines_growth_ratio=1.5,
    )

    def _cb(role: str, system: str, user: str) -> str:
        if role == "critic_t":
            return "- nit"
        if role == "architect_b":
            return "# Plan: B_BODY\n## Phase B"
        if role == "synthesizer":
            return bloated_ab
        # Judge: prefer AB by Borda (the demotion logic should override).
        return _judge_prefer(user, "AB_BLOATED")

    client = StubLLMClient(fn=_cb)
    artifact = tmp_path / "tournaments" / "oversize-demotion"
    t = Tournament(
        handler=PlanContentHandler(),
        client=client,
        cfg=cfg,
        artifact_dir=artifact,
        rng=random.Random(0),
    )
    final, history = await t.run("Repro task.", incumbent_initial)

    # The Borda winner was AB but demotion kicked in. The persisted
    # ``effective_winner`` should be A (since AB was demoted and A's score
    # exceeds B's in this scenario — judge always votes AB > A > B).
    assert len(history) == 2
    # Both passes hit the demotion path; both should be marked.
    for h in history:
        assert h.winner == "AB", "Borda raw winner should be AB"
        assert h.meta.get("ab_oversize_rejected") is True, (
            "Demotion must be tagged on the pass result for diagnosability."
        )
        assert h.meta.get("runaway_detected") is None, (
            "Demotion is NOT a runaway — that meta key must stay unset."
        )
        assert h.meta.get("runaway_trigger") is None
    # Two consecutive A demotions converge at k=2.
    assert all(h.meta.get("effective_winner") == "A" for h in history)
    # Final markdown stays the incumbent (A) because AB was demoted.
    assert final == incumbent_initial


@pytest.mark.asyncio
async def test_run_pass_keeps_ab_when_ratio_not_exceeded(tmp_path: Path) -> None:
    """Negative case: when AB stays within the ratio, no demotion happens."""
    incumbent_initial = "# Plan: foo\n" + "\n".join(f"## Phase {i}" for i in range(199))

    # 220 lines AB — within 1.5× of 200.
    modest_ab = (
        "# Plan: AB_MODEST\n"
        + "\n".join(f"## Phase {i}" for i in range(219))
    )
    assert len(modest_ab.splitlines()) == 220

    cfg = TournamentConfig(
        num_judges=1,
        convergence_k=2,
        max_rounds=2,
        max_plan_lines_growth_ratio=1.5,
    )

    def _cb(role: str, system: str, user: str) -> str:
        if role == "critic_t":
            return "- nit"
        if role == "architect_b":
            return "# Plan: B_BODY\n## Phase B"
        if role == "synthesizer":
            return modest_ab
        return _judge_prefer(user, "AB_MODEST")

    client = StubLLMClient(fn=_cb)
    artifact = tmp_path / "tournaments" / "no-demotion"
    t = Tournament(
        handler=PlanContentHandler(),
        client=client,
        cfg=cfg,
        artifact_dir=artifact,
        rng=random.Random(1),
    )
    _final, history = await t.run("Repro task.", incumbent_initial)

    # AB wins legitimately, demotion does NOT fire.
    assert len(history) >= 1
    for h in history:
        assert h.meta.get("ab_oversize_rejected") is None


@pytest.mark.asyncio
async def test_run_pass_disables_demotion_when_ratio_none(tmp_path: Path) -> None:
    """Regression: ``max_plan_lines_growth_ratio=None`` (legacy) disables the
    check — even an extreme AB growth keeps AB as winner.
    """
    incumbent_initial = "# Plan: foo\n## Phase 1"  # 2 lines
    bloated_ab = "# Plan: AB_BIG\n" + "\n".join(f"## Phase {i}" for i in range(99))

    cfg = TournamentConfig(
        num_judges=1,
        convergence_k=2,
        max_rounds=1,
        max_plan_lines_growth_ratio=None,
    )

    def _cb(role: str, system: str, user: str) -> str:
        if role == "critic_t":
            return "- nit"
        if role == "architect_b":
            return "# Plan: B_BODY\n## Phase B"
        if role == "synthesizer":
            return bloated_ab
        return _judge_prefer(user, "AB_BIG")

    client = StubLLMClient(fn=_cb)
    artifact = tmp_path / "tournaments" / "ratio-none"
    t = Tournament(
        handler=PlanContentHandler(),
        client=client,
        cfg=cfg,
        artifact_dir=artifact,
        rng=random.Random(2),
    )
    _final, history = await t.run("Repro task.", incumbent_initial)

    assert len(history) == 1
    assert history[0].meta.get("ab_oversize_rejected") is None
    assert history[0].meta.get("effective_winner") == "AB"
