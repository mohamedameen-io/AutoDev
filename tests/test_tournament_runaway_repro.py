"""End-to-end deterministic reproduction of the ``plan-47a530bd`` failure.

This test reproduces the failure shape observed in
``/Users/mohamedameen/git/unity/.autodev/tournaments/plan-47a530bd``:

  - judges always rank AB first (synthesizer-favored Borda outcome);
  - the synthesizer leaks a "Looking at both versions, X is..." preamble
    before the actual ``# Plan:`` heading;
  - every pass produces the same ``(winner, scores)`` pattern.

Pre-fix behavior: tournament runs all ``max_rounds`` without converging,
the saved incumbent contains the synthesizer's preamble, and Sonnet's
``error_max_turns`` failures retry transiently.

Post-fix behavior (asserted here):

  * Fix 5 — ``parse_synthesis`` strips the preamble. The stored incumbent
    starts with ``# Plan:``.
  * Fix 1 — when the synthesizer's output stabilizes (returns the same
    text two passes in a row), the hash short-circuit advances the
    streak even when AB wins by Borda.
  * Fix 6 — when the synthesizer keeps producing different content but
    scores are flat, the runaway detector terminates early.
  * Fix 4 — ``error_max_turns`` (deterministic subtype) does not retry.
  * Fix 2 + Fix 3 — :class:`AdapterLLMClient` honors per-role
    ``max_turns`` and translates an empty ``allowed_tools`` list to
    ``["Read"]``.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import pytest

from errors import TournamentError
from tournament import (
    AdapterLLMClient,
    StubLLMClient,
    Tournament,
    TournamentConfig,
)
from tournament.plan_tournament import PlanContentHandler


# ── Helpers (re-implementing the small bits we need) ─────────────────────


def _judge_prefer(prompt_text: str, prefer_marker: str) -> str:
    """Return ``RANKING: …`` placing the slot containing ``prefer_marker`` first.

    Mirrors :func:`tests.test_tournament_core._judge_prefer` semantics.
    """
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


# ── Repro: Fix 1 + Fix 5 cooperating ────────────────────────────────────


@pytest.mark.asyncio
async def test_repro_converges_via_hash_short_circuit_with_preamble_stripping(
    tmp_path: Path,
) -> None:
    """Fix 1 + Fix 5 turn the original divergent run into a converging one.

    Scenario:
      * synthesizer prepends "Looking at both versions..." preamble before
        a stable ``# Plan: STABLE`` body;
      * judges always pick AB.

    Without Fix 5 the saved incumbent would be the preamble + plan blob,
    and the next pass's synthesizer sees the preamble in its inputs.
    Without Fix 1 AB winning forever would mean the streak never advances.
    Together they cause convergence at ``streak >= convergence_k`` once
    the preamble-stripped body becomes byte-stable.
    """
    cfg = TournamentConfig(num_judges=3, convergence_k=2, max_rounds=10)

    initial = "# Plan: foo\n## Phase 1\n"
    stable_body = "# Plan: STABLE\n## Phase X\n"

    def _cb(role: str, system: str, user: str) -> str:
        if role == "critic_t":
            return "- nit: minor\n- nit: minor\n- nit: minor"
        if role == "architect_b":
            # Architect_b also opens with commentary that Fix 5 must strip.
            return (
                "Here is the revised plan addressing the criticisms:\n\n"
                "# Plan: B_BODY\n## Phase B\n"
            )
        if role == "synthesizer":
            # Always emit the same body, with a varying preamble in front.
            # Fix 5 strips the preamble → byte-stable plan content from the
            # second synthesizer call onwards.
            return (
                "Looking at both versions, X is the stronger base on nearly "
                "every technical dimension. The synthesis uses X as the "
                "structural base.\n\n" + stable_body
            )
        # judge
        # Prefer the slot whose body contains "STABLE" (the plan's distinctive
        # marker — only AB after Fix 5 strips the preamble).
        return _judge_prefer(user, "STABLE")

    client = StubLLMClient(fn=_cb)
    artifact = tmp_path / "tournaments" / "repro"
    handler = PlanContentHandler()
    t = Tournament(
        handler=handler,
        client=client,
        cfg=cfg,
        artifact_dir=artifact,
        rng=random.Random(0xDEAD),
    )
    final, history = await t.run("Repro task.", initial)

    # Fix 5: the stored incumbent must NOT start with a preamble.
    assert final.startswith("# Plan:")
    # The actual stored body is the post-Fix-5 stripped synthesizer output.
    expected_body = stable_body.rstrip()
    # Pass 1: incumbent flips from initial → stripped synth body. AB wins
    # by both Borda AND hash (different content). effective_winner=AB.
    # Pass 2: incumbent already == stable body; synthesizer returns the
    # same stripped body → hash equality → effective_winner=A. streak=1.
    # Pass 3: same → streak=2 → converge.
    assert len(history) == 3
    assert final == expected_body
    assert history[0].meta["effective_winner"] == "AB"
    assert history[1].meta["effective_winner"] == "A"
    assert history[2].meta["effective_winner"] == "A"
    # Raw winner stays AB throughout (Borda is unchanged; only the
    # streak interpretation changes).
    assert all(h.winner == "AB" for h in history)


# ── Repro: Fix 6 catches a divergent runaway ─────────────────────────────


@pytest.mark.asyncio
async def test_repro_runaway_detector_terminates_divergent_run(
    tmp_path: Path,
) -> None:
    """When the synthesizer keeps producing fresh-but-equivalent content,
    Fix 1 cannot help (hashes always differ). Fix 6 fires instead.
    """
    cfg = TournamentConfig(
        num_judges=3,
        convergence_k=2,
        max_rounds=10,
        score_stability_window=3,
        score_stability_max_delta=1,
    )

    state: dict[str, int] = {"synth_n": 0}

    def _cb(role: str, system: str, user: str) -> str:
        if role == "critic_t":
            return "- nit"
        if role == "architect_b":
            return "# Plan: B_VARIANT\n## Phase B\n"
        if role == "synthesizer":
            state["synth_n"] += 1
            # Different body each pass, distinct enough not to short-circuit
            # on hash, but the score pattern stays identical.
            return f"# Plan: AB_{state['synth_n']}\n## Phase AB_{state['synth_n']}\n"
        return _judge_prefer(user, f"# Plan: AB_{state['synth_n']}")

    client = StubLLMClient(fn=_cb)
    artifact = tmp_path / "tournaments" / "runaway-repro"
    t = Tournament(
        handler=PlanContentHandler(),
        client=client,
        cfg=cfg,
        artifact_dir=artifact,
        rng=random.Random(0xBEEF),
    )
    _final, history = await t.run("Repro task.", "# Plan: foo\n")

    # AB always wins by Borda; scores stay flat → runaway fires at pass 3.
    assert all(h.winner == "AB" for h in history)
    assert all(h.scores["AB"] == 9 for h in history)
    assert len(history) == 3
    assert history[-1].meta["runaway_detected"] is True


# ── Repro: Fix 4 short-circuits ``error_max_turns`` retries ─────────────


class _DeterministicFailureAdapter:
    """Adapter that mimics the ``error_max_turns`` shape Sonnet was hitting."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def execute(self, inv: Any) -> Any:
        self.calls.append(inv)

        class _Result:
            success = False
            text = ""
            error = "claude exited 1: error_max_turns"
            duration_s = 0.01
            subtype = "error_max_turns"

        return _Result()


@pytest.mark.asyncio
async def test_repro_error_max_turns_does_not_retry_on_deterministic_subtype(
    tmp_path: Path,
) -> None:
    """The actual ``architect_b`` failure mode from ``plan-47a530bd``."""
    adapter = _DeterministicFailureAdapter()
    client = AdapterLLMClient(
        adapter,
        cwd=tmp_path,
        max_attempts=5,
        role_max_turns={"architect_b": 5},
        role_allowed_tools={"architect_b": []},
    )
    with pytest.raises(TournamentError):
        await client.call(system="s", user="u", role="architect_b")
    # Pre-fix: 5 retries (substring "claude exited" matched _TRANSIENT_SUBSTRINGS).
    # Post-fix: exactly 1 attempt (subtype="error_max_turns" is deterministic).
    assert len(adapter.calls) == 1
    # And the per-role overrides flowed through.
    assert adapter.calls[0].max_turns == 5
    assert adapter.calls[0].allowed_tools == ["Read"]
