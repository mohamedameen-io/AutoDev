"""Resume-from-disk tests for the Tournament engine (Tier 2D).

Validates :meth:`tournament.state.TournamentArtifactStore.read_resume_state`
and the corresponding short-circuit / resume logic in :meth:`Tournament.run`.

Each test pre-populates ``tmp_path`` with the artifacts that a prior crashed
or partial tournament would have left, then runs ``Tournament.run`` with a
:class:`StubLLMClient` and asserts on:

  * the number of fresh LLM calls made (zero for completed; only for the
    remaining passes for partial),
  * the recovered incumbent text,
  * the resulting ``history`` length and final markdown.
"""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Any, Callable

import pytest

from tournament import (
    ContentHandler,
    PassResult,
    StubLLMClient,
    Tournament,
    TournamentArtifactStore,
    TournamentConfig,
)
from tournament.core import ResumeState
from tournament.prompts import JUDGE_RANK_3_PROMPT


# ── Test handler reused (mirrors test_tournament_core.StringHandler) ──


class StringHandler:
    """Treat T as a single markdown string; revisions/synthesis = the LLM text."""

    def render_for_critic(self, t: str, task_prompt: str) -> str:
        return f"TASK: {task_prompt}\n\nPROPOSAL:\n{t}"

    def render_for_architect_b(self, task_prompt: str, a: str, critic_text: str) -> str:
        return f"TASK: {task_prompt}\nA:\n{a}\nCRITIC:\n{critic_text}"

    def render_for_synthesizer(self, task_prompt: str, x: str, y: str) -> str:
        return f"TASK: {task_prompt}\nX:\n{x}\nY:\n{y}"

    def render_for_judge(
        self,
        task_prompt: str,
        v_a: str,
        v_b: str,
        v_ab: str,
        order_map: dict[int, str],
    ) -> str:
        versions = {"A": v_a, "B": v_b, "AB": v_ab}
        parts = [
            f"PROPOSAL {i}:\n---\n{versions[order_map[i]]}\n---" for i in (1, 2, 3)
        ]
        return JUDGE_RANK_3_PROMPT.format(
            task_prompt=task_prompt, judge_proposals="\n\n".join(parts)
        )

    def parse_revision(self, revision_text: str, original: str) -> str:
        return revision_text

    def parse_synthesis(self, synth_text: str, a: str, b: str) -> str:
        return synth_text

    def render_as_markdown(self, t: str) -> str:
        return t

    def hash(self, t: str) -> str:
        return hashlib.sha256(t.encode("utf-8")).hexdigest()[:16]


def _handler() -> ContentHandler[str]:
    return StringHandler()


# ── Helpers ─────────────────────────────────────────────────────────────────


def _prefix_map() -> dict[str, str]:
    return {
        "A": "MARK_A_ONLY",
        "B": "MARK_B_ONLY",
        "AB": "MARK_AB_ONLY",
    }


def _judge_prefer(prompt_text: str, prefer_prefix: str) -> str:
    """Emit a RANKING placing the proposal containing ``prefer_prefix`` first."""
    offsets: dict[int, int] = {}
    for slot in (1, 2, 3):
        marker = f"PROPOSAL {slot}:"
        idx = prompt_text.find(marker)
        if idx >= 0:
            offsets[slot] = idx
    ordered = sorted(offsets.items(), key=lambda kv: kv[1])
    slot_end: dict[int, int] = {}
    for i, (slot, start) in enumerate(ordered):
        end = ordered[i + 1][1] if i + 1 < len(ordered) else len(prompt_text)
        slot_end[slot] = end

    preferred_slot = None
    for slot, start in offsets.items():
        body = prompt_text[start : slot_end[slot]]
        if prefer_prefix in body:
            preferred_slot = slot
            break

    assert preferred_slot is not None
    others = [s for s in (1, 2, 3) if s != preferred_slot]
    return f"RANKING: {preferred_slot}, {others[0]}, {others[1]}"


def _role_defaults(role: str, user: str) -> str:
    if role == "critic_t":
        return "CRITIC: minor issues."
    if role == "architect_b":
        return "MARK_B_ONLY"
    if role == "synthesizer":
        return "MARK_AB_ONLY"
    raise AssertionError(f"unexpected role: {role}")


def _favor_label_factory(
    label: str, prefix_map: dict[str, str] | None = None
) -> Callable[[str, str, str], str]:
    pm = prefix_map or _prefix_map()

    def _cb(role: str, system: str, user: str) -> str:
        if role != "judge":
            return _role_defaults(role, user)
        return _judge_prefer(user, pm[label])

    return _cb


def _write_pass_result(
    artifact_dir: Path,
    pass_num: int,
    *,
    winner: str,
    version_a: str = "A_TEXT",
    version_b: str = "MARK_B_ONLY",
    version_ab: str = "MARK_AB_ONLY",
    critic: str = "CRITIC: ok.",
    incumbent_hash_before: str = "0000000000000000",
    incumbent_hash_after: str | None = None,
    valid_judges: int = 1,
) -> Path:
    """Write a complete pass directory with a parseable result.json."""
    pdir = artifact_dir / f"pass_{pass_num:02d}"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "version_a.md").write_text(version_a, encoding="utf-8")
    (pdir / "version_b.md").write_text(version_b, encoding="utf-8")
    (pdir / "version_ab.md").write_text(version_ab, encoding="utf-8")
    (pdir / "critic.md").write_text(critic, encoding="utf-8")
    handler = StringHandler()
    if incumbent_hash_after is None:
        winner_text = {"A": version_a, "B": version_b, "AB": version_ab}[winner]
        incumbent_hash_after = handler.hash(winner_text)
    result = PassResult(
        pass_num=pass_num,
        winner=winner,  # type: ignore[arg-type]
        scores={"A": 3, "B": 2, "AB": 1},
        valid_judges=valid_judges,
        elapsed_s=0.01,
        judge_details=[{"ranking": [winner, "A", "B"]}],
        incumbent_hash_before=incumbent_hash_before,
        incumbent_hash_after=incumbent_hash_after,
        meta={"timestamp": 1.0},
    )
    (pdir / "result.json").write_text(
        json.dumps(result.model_dump(mode="json"), indent=2), encoding="utf-8"
    )
    return pdir


# ── Tests ──────────────────────────────────────────────────────────────────


def test_no_artifacts_starts_fresh(tmp_path: Path) -> None:
    """Empty artifact dir → ``read_resume_state`` returns None."""
    store = TournamentArtifactStore(tmp_path / "empty")
    assert store.read_resume_state() is None


@pytest.mark.asyncio
async def test_resume_after_two_non_a_wins(tmp_path: Path) -> None:
    """Two completed non-A passes → resume from pass 3."""
    artifact = tmp_path / "tournaments" / "plan-resume2"
    artifact.mkdir(parents=True)

    initial = "MARK_A_ONLY_INITIAL"
    (artifact / "initial_a.md").write_text(initial, encoding="utf-8")

    # Pass 1: B wins
    _write_pass_result(artifact, 1, winner="B", version_a=initial)
    (artifact / "incumbent_after_01.md").write_text(
        "MARK_B_ONLY_AFTER_01", encoding="utf-8"
    )
    # Pass 2: AB wins
    _write_pass_result(artifact, 2, winner="AB", version_a="MARK_B_ONLY_AFTER_01")
    (artifact / "incumbent_after_02.md").write_text(
        "MARK_AB_ONLY_AFTER_02", encoding="utf-8"
    )

    # Sanity: read_resume_state should detect this.
    store = TournamentArtifactStore(artifact)
    rs = store.read_resume_state()
    assert rs is not None
    assert rs.completed is False
    assert rs.starting_pass_num == 3
    assert rs.streak == 0  # both non-A wins
    assert rs.incumbent_md == "MARK_AB_ONLY_AFTER_02"

    # Now run a Tournament — it should resume and only run pass 3.
    cfg = TournamentConfig(num_judges=1, convergence_k=2, max_rounds=5)
    client = StubLLMClient(fn=_favor_label_factory("A"))
    t = Tournament(
        handler=_handler(),
        client=client,
        cfg=cfg,
        artifact_dir=artifact,
        rng=random.Random(0),
    )
    final, history = await t.run("Task.", initial)

    # Only one new pass should have run (pass 3); we'd need 2 A-streak to
    # converge so passes 3 + 4 actually run before convergence at pass 4.
    pass_nums = [h.pass_num for h in history]
    assert min(pass_nums) == 3
    assert all(n >= 3 for n in pass_nums)

    # No critic call would have been made for passes 1 or 2.
    critic_calls = [c for c in client.calls if c["role"] == "critic_t"]
    # n==1 for the first new critic = pass 3
    assert all(c["n"] >= 1 for c in critic_calls)
    # No extra "fresh start" pass exists below pass 3.
    assert not (artifact / "pass_03").exists() or (artifact / "pass_03" / "result.json").exists()


@pytest.mark.asyncio
async def test_resume_with_a_win_streak(tmp_path: Path) -> None:
    """Two completed A-win passes with convergence_k=2 → immediately converge."""
    artifact = tmp_path / "tournaments" / "plan-streak"
    artifact.mkdir(parents=True)

    initial = "MARK_A_ONLY_INITIAL"
    (artifact / "initial_a.md").write_text(initial, encoding="utf-8")

    # Two A-wins (no incumbent_after files; A wins do not write them).
    _write_pass_result(artifact, 1, winner="A", version_a=initial)
    _write_pass_result(artifact, 2, winner="A", version_a=initial)

    store = TournamentArtifactStore(artifact)
    rs = store.read_resume_state()
    assert rs is not None
    assert rs.completed is False
    assert rs.starting_pass_num == 3
    assert rs.streak == 2
    # No prior non-A win → incumbent falls back to initial_a.md.
    assert rs.incumbent_md == initial

    # Run with convergence_k=2 → loop should immediately break, no LLM calls.
    cfg = TournamentConfig(num_judges=1, convergence_k=2, max_rounds=5)
    client = StubLLMClient(fn=_favor_label_factory("A"))
    t = Tournament(
        handler=_handler(),
        client=client,
        cfg=cfg,
        artifact_dir=artifact,
        rng=random.Random(0),
    )
    final, history = await t.run("Task.", initial)

    # No new LLM calls — streak=2 already meets convergence_k=2.
    # The loop body never executes.
    assert client.calls == []
    assert history == []
    # final_output.md was written by the converged-exit branch.
    assert (artifact / "final_output.md").exists()
    assert final == initial


@pytest.mark.asyncio
async def test_resume_partial_pass_dir_ignored(tmp_path: Path) -> None:
    """A pass dir with no result.json is ignored; resume picks the highest complete pass."""
    artifact = tmp_path / "tournaments" / "plan-partial"
    artifact.mkdir(parents=True)

    initial = "MARK_A_ONLY_INITIAL"
    (artifact / "initial_a.md").write_text(initial, encoding="utf-8")

    # Pass 1 + 2 complete (B wins each).
    _write_pass_result(artifact, 1, winner="B", version_a=initial)
    (artifact / "incumbent_after_01.md").write_text("MARK_B_ONLY_01", encoding="utf-8")
    _write_pass_result(artifact, 2, winner="B", version_a="MARK_B_ONLY_01")
    (artifact / "incumbent_after_02.md").write_text("MARK_B_ONLY_02", encoding="utf-8")

    # Pass 3 partial: only critic.md, no result.json.
    pass3 = artifact / "pass_03"
    pass3.mkdir()
    (pass3 / "critic.md").write_text("orphaned critic", encoding="utf-8")

    store = TournamentArtifactStore(artifact)
    rs = store.read_resume_state()
    assert rs is not None
    assert rs.starting_pass_num == 3
    assert rs.incumbent_md == "MARK_B_ONLY_02"
    assert rs.streak == 0

    # Run; pass 3 will be re-run and overwrite the orphaned critic.
    cfg = TournamentConfig(num_judges=1, convergence_k=1, max_rounds=4)
    client = StubLLMClient(fn=_favor_label_factory("A"))
    t = Tournament(
        handler=_handler(),
        client=client,
        cfg=cfg,
        artifact_dir=artifact,
        rng=random.Random(0),
    )
    await t.run("Task.", initial)

    # The orphaned critic.md should have been overwritten.
    assert (pass3 / "critic.md").read_text(encoding="utf-8") != "orphaned critic"
    # result.json should now exist.
    assert (pass3 / "result.json").exists()


def test_resume_winner_b_with_missing_incumbent_after(tmp_path: Path) -> None:
    """Crash window: result.json with winner=B exists but incumbent_after_02.md missing.

    Resume should fall back to ``pass_02/version_b.md``.
    """
    artifact = tmp_path / "tournaments" / "plan-crash"
    artifact.mkdir(parents=True)
    initial = "MARK_A_ONLY_INITIAL"
    (artifact / "initial_a.md").write_text(initial, encoding="utf-8")

    # Pass 1 complete with incumbent_after_01.
    _write_pass_result(artifact, 1, winner="B", version_a=initial)
    (artifact / "incumbent_after_01.md").write_text("MARK_B_ONLY_01", encoding="utf-8")

    # Pass 2 complete (result.json exists), but incumbent_after_02.md absent.
    _write_pass_result(
        artifact,
        2,
        winner="B",
        version_a="MARK_B_ONLY_01",
        version_b="MARK_B_ONLY_PASS2",
    )
    # NOTE: incumbent_after_02.md is intentionally NOT written.

    store = TournamentArtifactStore(artifact)
    rs = store.read_resume_state()
    assert rs is not None
    assert rs.starting_pass_num == 3
    assert rs.streak == 0
    assert rs.incumbent_md == "MARK_B_ONLY_PASS2"


@pytest.mark.asyncio
async def test_resume_completed_returns_final(tmp_path: Path) -> None:
    """If ``final_output.md`` exists, ``Tournament.run`` returns it without LLM calls."""
    artifact = tmp_path / "tournaments" / "plan-done"
    artifact.mkdir(parents=True)

    initial = "MARK_A_ONLY_INITIAL"
    (artifact / "initial_a.md").write_text(initial, encoding="utf-8")
    (artifact / "final_output.md").write_text("FINAL_RESULT", encoding="utf-8")
    # Optionally write history.json too.
    (artifact / "history.json").write_text("[]", encoding="utf-8")

    store = TournamentArtifactStore(artifact)
    rs = store.read_resume_state()
    assert rs is not None
    assert rs.completed is True
    assert rs.final_md == "FINAL_RESULT"

    cfg = TournamentConfig(num_judges=1, convergence_k=1, max_rounds=3)
    client = StubLLMClient(fn=_favor_label_factory("A"))
    t = Tournament(
        handler=_handler(),
        client=client,
        cfg=cfg,
        artifact_dir=artifact,
        rng=random.Random(0),
    )
    final, history = await t.run("Task.", initial)

    assert final == "FINAL_RESULT"
    assert client.calls == []  # no LLM calls
    assert history == []  # history.json was empty


@pytest.mark.asyncio
async def test_resume_initial_mismatch_starts_fresh(tmp_path: Path) -> None:
    """If initial_a.md hash differs from the current ``initial`` arg → fresh start."""
    artifact = tmp_path / "tournaments" / "plan-mismatch"
    artifact.mkdir(parents=True)

    # Pre-populate with X content.
    (artifact / "initial_a.md").write_text("INITIAL_X", encoding="utf-8")
    _write_pass_result(artifact, 1, winner="B", version_a="INITIAL_X")
    (artifact / "incumbent_after_01.md").write_text("MARK_B_ONLY_X", encoding="utf-8")

    # Now run with a DIFFERENT initial (Y).
    new_initial = "MARK_A_ONLY_FRESH"
    cfg = TournamentConfig(num_judges=1, convergence_k=2, max_rounds=2)
    client = StubLLMClient(fn=_favor_label_factory("A"))
    t = Tournament(
        handler=_handler(),
        client=client,
        cfg=cfg,
        artifact_dir=artifact,
        rng=random.Random(0),
    )
    _, history = await t.run("Task.", new_initial)

    # Fresh start: initial_a.md was overwritten with the new initial.
    assert (artifact / "initial_a.md").read_text(encoding="utf-8") == new_initial
    # All passes ran from pass 1.
    pass_nums = [h.pass_num for h in history]
    assert min(pass_nums) == 1


@pytest.mark.asyncio
async def test_deterministic_rng_across_runs(tmp_path: Path) -> None:
    """Same RNG seed + same stub responses → byte-identical artifacts.

    Locks in the RNG-seed contract that backs deterministic resume.
    """
    cfg = TournamentConfig(num_judges=3, convergence_k=2, max_rounds=3)

    async def _run_once(dir_: Path) -> None:
        client = StubLLMClient(fn=_favor_label_factory("A"))
        t = Tournament(
            handler=_handler(),
            client=client,
            cfg=cfg,
            artifact_dir=dir_,
            rng=random.Random(42),
        )
        await t.run("Task.", "MARK_A_ONLY_INITIAL")

    a = tmp_path / "run_a"
    b = tmp_path / "run_b"
    await _run_once(a)
    await _run_once(b)

    for name in ("initial_a.md", "final_output.md"):
        assert (a / name).read_bytes() == (b / name).read_bytes()
    for p in (1, 2):
        for name in ("version_a.md", "critic.md", "version_b.md", "version_ab.md"):
            assert (a / f"pass_{p:02d}" / name).read_bytes() == (
                b / f"pass_{p:02d}" / name
            ).read_bytes()
