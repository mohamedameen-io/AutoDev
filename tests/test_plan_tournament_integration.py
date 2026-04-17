"""End-to-end tests driving :class:`Tournament` with :class:`PlanContentHandler`.

Uses :class:`StubLLMClient` with scripted role responses. Per-pass author_b
and synthesizer outputs carry distinct, non-overlapping markers so a
callback judge can reliably identify which proposal slot contains the
canonical A / B / AB label and emit a ``RANKING:`` that favors the target
label for that pass.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from tournament import (
    PlanContentHandler,
    StubLLMClient,
    Tournament,
    TournamentConfig,
)


# Initial incumbent carries a unique marker so we can detect it even after
# several passes.
INITIAL_MARK = "PLANMARK_INIT_Q1"
INITIAL_PLAN = f"# Plan: initial\n{INITIAL_MARK}\n\n## Phase 1: start\n- s1\n"


# ── Helpers ──────────────────────────────────────────────────────────────


def _mark_for(role: str, pass_num: int) -> str:
    """Return a globally unique marker for role output in pass ``pass_num``.

    Markers are mutually non-overlapping (no substring relationships) so the
    judge callback can unambiguously find the slot that contains the label it
    wants to favor.
    """
    return f"PLANMARK_{role.upper()}_P{pass_num}"


def _body_for(role: str, pass_num: int) -> str:
    return f"# Plan: {role} pass{pass_num}\n{_mark_for(role, pass_num)}\n\n## Phase 1: x\n- y\n"


def _slot_containing(prompt_text: str, marker: str) -> int | None:
    """Return which PROPOSAL N: slot contains ``marker`` in a judge prompt."""
    offsets: dict[int, int] = {}
    for slot in (1, 2, 3):
        idx = prompt_text.find(f"PROPOSAL {slot}:")
        if idx >= 0:
            offsets[slot] = idx
    ordered = sorted(offsets.items(), key=lambda kv: kv[1])
    slot_end = {}
    for i, (slot, start) in enumerate(ordered):
        slot_end[slot] = ordered[i + 1][1] if i + 1 < len(ordered) else len(prompt_text)
    for slot, start in offsets.items():
        body = prompt_text[start : slot_end[slot]]
        if marker in body:
            return slot
    return None


def _build_callback(per_pass_targets: list[str], num_judges: int):
    """Return an ``fn(role, system, user) -> str`` stub callback.

    State tracked:
      - ``pass_idx``: current pass (0-based), derived from judge call count.
      - ``incumbent_marker``: marker carried by the current A label. Updated
        at the end of each pass based on that pass's winner.

    Per pass the author_b output uses marker ``_mark_for("b", pass)`` and the
    synthesizer uses ``_mark_for("synth", pass)``.
    """
    state = {
        "judge_calls": 0,
        "incumbent_marker": INITIAL_MARK,
    }

    def _cb(role: str, system: str, user: str) -> str:
        # Compute current pass (0-based) from judge calls — author_b/synth/critic
        # calls happen before the judges in a pass, but by the time judge is
        # called we've exhausted those 3 other roles.
        pass_idx = state["judge_calls"] // num_judges

        if role == "critic_t":
            return f"Critic pass{pass_idx + 1}: two issues identified."
        if role == "architect_b":
            return _body_for("b", pass_idx + 1)
        if role == "synthesizer":
            return _body_for("synth", pass_idx + 1)
        if role == "judge":
            target = per_pass_targets[pass_idx]
            # Map canonical label -> marker present in the proposal slots.
            label_markers = {
                "A": state["incumbent_marker"],
                "B": _mark_for("b", pass_idx + 1),
                "AB": _mark_for("synth", pass_idx + 1),
            }
            slot = _slot_containing(user, label_markers[target])
            assert slot is not None, (
                f"could not locate slot for label {target} "
                f"(marker={label_markers[target]!r}) in pass {pass_idx + 1}"
            )
            others = [s for s in (1, 2, 3) if s != slot]
            ranking = f"RANKING: {slot}, {others[0]}, {others[1]}"
            state["judge_calls"] += 1
            # If this was the last judge in the pass, advance incumbent marker
            # for the NEXT pass based on the target of the CURRENT pass.
            if state["judge_calls"] % num_judges == 0:
                if target == "B":
                    state["incumbent_marker"] = _mark_for("b", pass_idx + 1)
                elif target == "AB":
                    state["incumbent_marker"] = _mark_for("synth", pass_idx + 1)
                # If target == "A", marker stays the same.
            return f"Evaluation.\n\n{ranking}"
        raise AssertionError(f"unexpected role: {role}")

    return _cb


# ── Tests ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_converges_after_ab_win_then_two_a_wins(tmp_path: Path) -> None:
    """Pass 1: AB wins. Pass 2-3: A wins (streak=2) → converge at pass 3.

    Verifies:
      - history length and winners,
      - final incumbent carries the synthesizer's pass-1 marker (AB's body),
      - artifacts written (initial_a, incumbent_after_01, per-pass dirs,
        final_output, history.json),
      - NO incumbent_after_02 (A won → incumbent unchanged).
    """
    cfg = TournamentConfig(num_judges=2, convergence_k=2, max_rounds=5)
    client = StubLLMClient(fn=_build_callback(["AB", "A", "A"], num_judges=cfg.num_judges))
    artifact = tmp_path / "tournaments" / "plan-integration"

    t = Tournament(
        handler=PlanContentHandler(),
        client=client,
        cfg=cfg,
        artifact_dir=artifact,
        rng=random.Random(42),
    )
    final, history = await t.run(task_prompt="Refine this plan.", initial=INITIAL_PLAN)

    assert [h.winner for h in history] == ["AB", "A", "A"]
    assert len(history) == 3
    # Final incumbent must carry pass-1 synth marker and NOT the initial one.
    assert _mark_for("synth", 1) in final
    assert INITIAL_MARK not in final

    assert (artifact / "initial_a.md").exists()
    assert (artifact / "final_output.md").exists()
    assert (artifact / "history.json").exists()
    for pass_num in (1, 2, 3):
        pdir = artifact / f"pass_{pass_num:02d}"
        assert (pdir / "version_a.md").exists()
        assert (pdir / "critic.md").exists()
        assert (pdir / "version_b.md").exists()
        assert (pdir / "version_ab.md").exists()
        assert (pdir / "result.json").exists()
    assert (artifact / "incumbent_after_01.md").exists()
    assert not (artifact / "incumbent_after_02.md").exists()
    assert not (artifact / "incumbent_after_03.md").exists()


@pytest.mark.asyncio
async def test_all_a_wins_converges_at_k(tmp_path: Path) -> None:
    """If judges always favor A, the tournament converges at pass ``convergence_k``."""
    cfg = TournamentConfig(num_judges=3, convergence_k=2, max_rounds=10)
    client = StubLLMClient(fn=_build_callback(["A", "A"], num_judges=cfg.num_judges))
    t = Tournament(
        handler=PlanContentHandler(),
        client=client,
        cfg=cfg,
        artifact_dir=tmp_path / "all-a",
        rng=random.Random(0),
    )
    final, history = await t.run(task_prompt="Refine.", initial=INITIAL_PLAN)
    assert len(history) == 2
    assert all(h.winner == "A" for h in history)
    assert final == INITIAL_PLAN


@pytest.mark.asyncio
async def test_history_json_structure(tmp_path: Path) -> None:
    """``history.json`` mirrors :class:`PassResult.model_dump(mode="json")`."""
    cfg = TournamentConfig(num_judges=1, convergence_k=1, max_rounds=1)
    client = StubLLMClient(fn=_build_callback(["A"], num_judges=cfg.num_judges))
    t = Tournament(
        handler=PlanContentHandler(),
        client=client,
        cfg=cfg,
        artifact_dir=tmp_path / "tt",
        rng=random.Random(0),
    )
    _, history = await t.run(task_prompt="x", initial=INITIAL_PLAN)
    raw = json.loads((tmp_path / "tt" / "history.json").read_text())
    assert len(raw) == 1
    entry = raw[0]
    assert entry["winner"] == "A"
    assert entry["pass_num"] == 1
    assert set(entry["scores"].keys()) == {"A", "B", "AB"}
    handler = PlanContentHandler()
    assert entry["incumbent_hash_before"] == handler.hash(INITIAL_PLAN)
    assert entry["incumbent_hash_after"] == entry["incumbent_hash_before"]


@pytest.mark.asyncio
async def test_final_content_matches_synth_body(tmp_path: Path) -> None:
    """When AB wins on pass 1 then A wins through convergence, final == synth pass-1."""
    cfg = TournamentConfig(num_judges=2, convergence_k=2, max_rounds=5)
    client = StubLLMClient(fn=_build_callback(["AB", "A", "A"], num_judges=cfg.num_judges))
    t = Tournament(
        handler=PlanContentHandler(),
        client=client,
        cfg=cfg,
        artifact_dir=tmp_path / "ab-then-a",
        rng=random.Random(7),
    )
    final, _ = await t.run(task_prompt="Refine.", initial=INITIAL_PLAN)
    # parse_synthesis strips whitespace, so compare against the stripped body.
    assert final == _body_for("synth", 1).strip()
    assert (tmp_path / "ab-then-a" / "final_output.md").read_text() == final


@pytest.mark.asyncio
async def test_initial_a_md_written_verbatim(tmp_path: Path) -> None:
    """initial_a.md should match the original incumbent markdown byte-for-byte."""
    cfg = TournamentConfig(num_judges=1, convergence_k=1, max_rounds=1)
    client = StubLLMClient(fn=_build_callback(["A"], num_judges=cfg.num_judges))
    artifact = tmp_path / "initial"
    t = Tournament(
        handler=PlanContentHandler(),
        client=client,
        cfg=cfg,
        artifact_dir=artifact,
        rng=random.Random(0),
    )
    await t.run(task_prompt="x", initial=INITIAL_PLAN)
    assert (artifact / "initial_a.md").read_text() == INITIAL_PLAN
