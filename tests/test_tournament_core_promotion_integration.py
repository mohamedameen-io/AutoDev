"""Tournament.run integration tests for the v0.16.0 promotion ladder.

Drives :class:`Tournament` end-to-end with a stub :class:`LLMClient` so the
ladder behavior is observed via on-disk artifacts (the sidecar JSON
written next to ``incumbent_after_NN.md``).

The fixture replays a deterministic sequence:
  * pass 1 — non-A winner ("B" via stubbed judges).
  * pass 2 — second non-A winner over the now-incumbent candidate.

Behaviour matrix:
  * ``promotion_grade_enabled=False`` (default) — sidecar is still written
    (legacy default ``dev_best``) but no ladder semantics; this is the
    regression guard.
  * ``promotion_grade_enabled=True``  — sidecar grade transitions
    ``dev_best`` → ``repeated`` across the two non-A wins.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from tournament.core import (
    ContentHandler,
    Tournament,
    TournamentConfig,
)


# ── stub LLM + handler ─────────────────────────────────────────────────────


class _StubLLMClient:
    """Deterministic responses keyed on role.

    The judge response sets RANKING with display position 1 first.
    Combined with the seeded RNG, that places "B" at the winning slot
    each pass. Architect_b emits a unique payload per call so the
    incumbent hash actually changes between passes (otherwise the
    hash-equality short-circuit forces ``effective_winner=A``).
    """

    def __init__(self) -> None:
        self.last_pid = None
        self._architect_b_calls = 0
        self._synth_calls = 0

    async def call(
        self,
        *,
        system: str,
        user: str,
        role: str,
        model: str | None = None,
    ) -> str:
        if role == "critic_t":
            return "## Critique\n- replace foo with bar"
        if role == "architect_b":
            self._architect_b_calls += 1
            return f"BAR-VERSION-{self._architect_b_calls}"
        if role == "synthesizer":
            self._synth_calls += 1
            return f"AB-VERSION-{self._synth_calls}"
        if role == "judge":
            # Top-display-position wins; the seeded RNG places B at pos 1.
            return "I prefer B for clarity.\n\nRANKING: 1, 2, 3"
        return ""


class _StubHandler:
    """Minimal ContentHandler for plain string content."""

    def render_for_critic(self, t: str, task_prompt: str) -> str:
        return f"TASK: {task_prompt}\n\nINCUMBENT:\n{t}"

    def render_for_architect_b(
        self, task_prompt: str, a: str, critic_text: str
    ) -> str:
        return f"REVISE based on:\n{critic_text}\n\nORIGINAL:\n{a}"

    def render_for_synthesizer(self, task_prompt: str, x: str, y: str) -> str:
        return f"SYNTH X:\n{x}\nY:\n{y}"

    def render_for_judge(
        self,
        task_prompt: str,
        v_a: str,
        v_b: str,
        v_ab: str,
        order_map: dict[int, str],
    ) -> str:
        return f"JUDGE: pos1={order_map[1]} pos2={order_map[2]} pos3={order_map[3]}"

    def parse_revision(self, revision_text: str, original: str) -> str:
        return revision_text

    def parse_synthesis(self, synth_text: str, a: str, b: str) -> str:
        return synth_text

    def render_as_markdown(self, t: str) -> str:
        return t

    def hash(self, t: str) -> str:
        # Identity hash: include the full payload so byte-distinct
        # content produces distinct hashes (otherwise Tournament's
        # ``no_change`` short-circuit collapses successive non-A wins
        # into A-wins on a hash collision).
        return f"H:{len(t)}:{t}"


def _force_b_first_rng() -> random.Random:
    """Return an RNG seeded so the first two passes both put "B" at
    judge-display position 1.

    Tournament.run_pass draws once for the synthesizer coin flip and once
    for the judge shuffle, in that order. Seed 13 was picked offline so
    that — with this draw order and only one judge — B lands at display
    position 1 in both pass 1 and pass 2. With the stub judge always
    emitting ``RANKING: 1, 2, 3``, top-display-position wins, so B wins
    each pass deterministically.
    """
    return random.Random(13)


_TCFG = TournamentConfig(
    num_judges=1,
    convergence_k=999,  # never converge — we want ≥2 passes
    max_rounds=2,
    max_parallel_subprocesses=1,
)


# ── tests ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_promotion_disabled_writes_incumbent_directly(
    tmp_path: Path,
) -> None:
    """Default (promotion_grade_enabled=False) preserves legacy semantics.

    Sidecar is still written (default ``dev_best``) but with no ladder
    transitions: pass 1 and pass 2 are both ``dev_best`` because the
    feature is off.
    """
    cfg = TournamentConfig(**{**_TCFG.__dict__, "max_rounds": 2})
    t = Tournament(
        handler=_StubHandler(),
        client=_StubLLMClient(),
        cfg=cfg,
        artifact_dir=tmp_path,
        rng=_force_b_first_rng(),
    )
    await t.run("task prompt", "INITIAL")

    # Pass 1's incumbent_after_01.md exists (B won).
    md_01 = tmp_path / "incumbent_after_01.md"
    grade_01 = tmp_path / "incumbent_after_01.grade.json"
    assert md_01.exists()
    assert grade_01.exists()
    assert json.loads(grade_01.read_text())["grade"] == "dev_best"

    # Pass 2's incumbent_after_02.md exists (B won again over previous).
    md_02 = tmp_path / "incumbent_after_02.md"
    grade_02 = tmp_path / "incumbent_after_02.grade.json"
    assert md_02.exists()
    # Promotion disabled — grade stays dev_best.
    assert json.loads(grade_02.read_text())["grade"] == "dev_best"


@pytest.mark.asyncio
async def test_promotion_enabled_first_winner_graded_dev_best(
    tmp_path: Path,
) -> None:
    """With promotion enabled, the first non-A winner is graded ``dev_best``.

    The grade reflects the rung the candidate currently SITS AT — pre-
    confirmation, it's the bottom of the ladder.
    """
    cfg = TournamentConfig(
        **{
            **_TCFG.__dict__,
            "max_rounds": 1,
            "promotion_grade_enabled": True,
        }
    )
    t = Tournament(
        handler=_StubHandler(),
        client=_StubLLMClient(),
        cfg=cfg,
        artifact_dir=tmp_path,
        rng=_force_b_first_rng(),
    )
    await t.run("task prompt", "INITIAL")

    grade_01 = tmp_path / "incumbent_after_01.grade.json"
    assert grade_01.exists()
    assert json.loads(grade_01.read_text())["grade"] == "dev_best"


@pytest.mark.asyncio
async def test_promotion_enabled_second_win_promotes_to_repeated(
    tmp_path: Path,
) -> None:
    """Second consecutive non-A win confirms the candidate → grade ``repeated``.

    Plan rule: ``dev_best → demand_repeat → repeated``. The two-pass
    fixture stands in for the demanded repeat: the second non-A win
    confirms the first.
    """
    cfg = TournamentConfig(
        **{
            **_TCFG.__dict__,
            "max_rounds": 2,
            "promotion_grade_enabled": True,
        }
    )
    t = Tournament(
        handler=_StubHandler(),
        client=_StubLLMClient(),
        cfg=cfg,
        artifact_dir=tmp_path,
        rng=_force_b_first_rng(),
    )
    await t.run("task prompt", "INITIAL")

    grade_01 = json.loads(
        (tmp_path / "incumbent_after_01.grade.json").read_text()
    )["grade"]
    grade_02 = json.loads(
        (tmp_path / "incumbent_after_02.grade.json").read_text()
    )["grade"]
    assert grade_01 == "dev_best"
    assert grade_02 == "repeated"
