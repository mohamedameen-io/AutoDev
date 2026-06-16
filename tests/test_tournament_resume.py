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


# ── Tier 3 partial-state pre-populator helpers ──────────────────────────────


def _write_version_a(artifact_dir: Path, pass_num: int, content: str) -> Path:
    """Pre-populate pass_NN/version_a.md."""
    pdir = artifact_dir / f"pass_{pass_num:02d}"
    pdir.mkdir(parents=True, exist_ok=True)
    p = pdir / "version_a.md"
    p.write_text(content, encoding="utf-8")
    return p


def _write_critic(artifact_dir: Path, pass_num: int, content: str) -> Path:
    """Pre-populate pass_NN/critic.md."""
    pdir = artifact_dir / f"pass_{pass_num:02d}"
    pdir.mkdir(parents=True, exist_ok=True)
    p = pdir / "critic.md"
    p.write_text(content, encoding="utf-8")
    return p


def _write_version_b(artifact_dir: Path, pass_num: int, content: str) -> Path:
    """Pre-populate pass_NN/version_b.md."""
    pdir = artifact_dir / f"pass_{pass_num:02d}"
    pdir.mkdir(parents=True, exist_ok=True)
    p = pdir / "version_b.md"
    p.write_text(content, encoding="utf-8")
    return p


def _write_version_ab(artifact_dir: Path, pass_num: int, content: str) -> Path:
    """Pre-populate pass_NN/version_ab.md."""
    pdir = artifact_dir / f"pass_{pass_num:02d}"
    pdir.mkdir(parents=True, exist_ok=True)
    p = pdir / "version_ab.md"
    p.write_text(content, encoding="utf-8")
    return p


def _write_synth_meta(
    artifact_dir: Path, pass_num: int, x_label: str, y_label: str
) -> Path:
    """Pre-populate pass_NN/synth_meta.json."""
    pdir = artifact_dir / f"pass_{pass_num:02d}"
    pdir.mkdir(parents=True, exist_ok=True)
    p = pdir / "synth_meta.json"
    p.write_text(
        json.dumps({"x_label": x_label, "y_label": y_label}, indent=2),
        encoding="utf-8",
    )
    return p


def _write_judge_order(
    artifact_dir: Path,
    pass_num: int,
    judge_index: int,
    order: dict[int, str],
) -> Path:
    """Pre-populate pass_NN/judges/<i>_order.json (string keys for JSON)."""
    pdir = artifact_dir / f"pass_{pass_num:02d}" / "judges"
    pdir.mkdir(parents=True, exist_ok=True)
    p = pdir / f"{judge_index}_order.json"
    serialisable = {str(k): v for k, v in order.items()}
    p.write_text(json.dumps(serialisable, indent=2), encoding="utf-8")
    return p


def _write_judge_response(
    artifact_dir: Path,
    pass_num: int,
    judge_index: int,
    response: dict[str, Any],
) -> Path:
    """Pre-populate pass_NN/judges/<i>_response.json."""
    pdir = artifact_dir / f"pass_{pass_num:02d}" / "judges"
    pdir.mkdir(parents=True, exist_ok=True)
    p = pdir / f"{judge_index}_response.json"
    p.write_text(json.dumps(response, indent=2), encoding="utf-8")
    return p


def _make_artifact_dir(tmp_path: Path, name: str = "plan-tier3") -> Path:
    """Create a fresh artifact dir under ``tmp_path/tournaments/<name>``."""
    artifact = tmp_path / "tournaments" / name
    artifact.mkdir(parents=True)
    return artifact


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
async def test_resume_partial_pass_dir_completes_pass(tmp_path: Path) -> None:
    """Tier 3: a pass dir with partial per-role artifacts resumes mid-pass.

    Pre-populate pass_03 with ``critic.md`` only (and the on-disk
    ``version_a.md`` matching the recovered incumbent so the stale-guard
    doesn't trip). The Tournament should resume pass 3 by skipping CRITIC and
    running ARCHITECT_B / SYNTHESIZER / JUDGES.
    """
    artifact = tmp_path / "tournaments" / "plan-partial"
    artifact.mkdir(parents=True)

    initial = "MARK_A_ONLY_INITIAL"
    (artifact / "initial_a.md").write_text(initial, encoding="utf-8")

    # Pass 1 + 2 complete (B wins each).
    _write_pass_result(artifact, 1, winner="B", version_a=initial)
    (artifact / "incumbent_after_01.md").write_text("MARK_B_ONLY_01", encoding="utf-8")
    _write_pass_result(artifact, 2, winner="B", version_a="MARK_B_ONLY_01")
    (artifact / "incumbent_after_02.md").write_text("MARK_B_ONLY_02", encoding="utf-8")

    # Pass 3 partial: critic.md + version_a.md (matching the recovered
    # incumbent so the stale-version_a guard does NOT discard the partial).
    pre_populated_critic = "PARTIAL_CRITIC_FROM_DISK"
    _write_version_a(artifact, 3, "MARK_B_ONLY_02")
    _write_critic(artifact, 3, pre_populated_critic)

    # Run; pass 3 should resume by skipping CRITIC.
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

    # CRITIC was skipped: zero stub calls for that role.
    critic_calls = [c for c in client.calls if c["role"] == "critic_t"]
    assert critic_calls == []

    # ARCHITECT_B / SYNTHESIZER / JUDGES all fired at least once.
    assert any(c["role"] == "architect_b" for c in client.calls)
    assert any(c["role"] == "synthesizer" for c in client.calls)
    assert any(c["role"] == "judge" for c in client.calls)

    # result.json exists; critic.md is the original pre-populated content.
    pass3 = artifact / "pass_03"
    assert (pass3 / "result.json").exists()
    assert (pass3 / "critic.md").read_text(encoding="utf-8") == pre_populated_critic


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
async def test_deterministic_rng_across_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Same RNG seed + same stub responses → byte-identical artifacts.

    Locks in the RNG-seed contract that backs deterministic resume. Tier 3
    extension: also asserts no ``tournament.partial_pass_resume`` log fires
    during a fresh run — the partial-resume code path is gated to actual
    resumes only.
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

    # Tier 3 boundary: no partial_pass_resume log during a fresh run.
    out = capsys.readouterr().out
    assert "tournament.partial_pass_resume" not in out, (
        f"unexpected partial_pass_resume log in fresh run; got: {out[-2000:]}"
    )


# ── Tier 3: mid-pass per-role resume tests ─────────────────────────────────


def _setup_two_b_wins(artifact: Path, initial: str) -> str:
    """Pre-populate passes 1 and 2 (both B-wins). Returns the pass-2 incumbent."""
    (artifact / "initial_a.md").write_text(initial, encoding="utf-8")
    _write_pass_result(artifact, 1, winner="B", version_a=initial)
    inc1 = "MARK_B_ONLY_01"
    (artifact / "incumbent_after_01.md").write_text(inc1, encoding="utf-8")
    _write_pass_result(artifact, 2, winner="B", version_a=inc1)
    inc2 = "MARK_B_ONLY_02"
    (artifact / "incumbent_after_02.md").write_text(inc2, encoding="utf-8")
    return inc2


@pytest.mark.asyncio
async def test_partial_resume_after_critic_only(tmp_path: Path) -> None:
    """Pre-populate pass 3 with critic.md only → CRITIC skipped, others run."""
    artifact = _make_artifact_dir(tmp_path, "partial-critic-only")
    initial = "MARK_A_ONLY_INITIAL"
    inc2 = _setup_two_b_wins(artifact, initial)

    # Partial pass 3: version_a.md (matches incumbent) + critic.md only.
    _write_version_a(artifact, 3, inc2)
    _write_critic(artifact, 3, "PARTIAL_CRITIC")

    cfg = TournamentConfig(num_judges=3, convergence_k=1, max_rounds=4)
    client = StubLLMClient(fn=_favor_label_factory("A"))
    t = Tournament(
        handler=_handler(),
        client=client,
        cfg=cfg,
        artifact_dir=artifact,
        rng=random.Random(0),
    )
    await t.run("Task.", initial)

    counts: dict[str, int] = {}
    for c in client.calls:
        counts[c["role"]] = counts.get(c["role"], 0) + 1

    assert counts.get("critic_t", 0) == 0, "CRITIC must not run when critic.md exists"
    assert counts.get("architect_b", 0) >= 1
    assert counts.get("synthesizer", 0) >= 1
    assert counts.get("judge", 0) >= 3

    # result.json was written; pass-3 resumed.
    assert (artifact / "pass_03" / "result.json").exists()


@pytest.mark.asyncio
async def test_partial_resume_after_architect_b(tmp_path: Path) -> None:
    """Pre-populate critic.md + version_b.md → CRITIC + ARCHITECT_B skipped."""
    artifact = _make_artifact_dir(tmp_path, "partial-arch-b")
    initial = "MARK_A_ONLY_INITIAL"
    inc2 = _setup_two_b_wins(artifact, initial)

    _write_version_a(artifact, 3, inc2)
    _write_critic(artifact, 3, "PARTIAL_CRITIC")
    _write_version_b(artifact, 3, "PARTIAL_MARK_B_ONLY")

    cfg = TournamentConfig(num_judges=3, convergence_k=1, max_rounds=4)
    client = StubLLMClient(fn=_favor_label_factory("A"))
    t = Tournament(
        handler=_handler(),
        client=client,
        cfg=cfg,
        artifact_dir=artifact,
        rng=random.Random(0),
    )
    await t.run("Task.", initial)

    counts: dict[str, int] = {}
    for c in client.calls:
        counts[c["role"]] = counts.get(c["role"], 0) + 1

    assert counts.get("critic_t", 0) == 0
    assert counts.get("architect_b", 0) == 0
    assert counts.get("synthesizer", 0) >= 1
    assert counts.get("judge", 0) >= 3
    assert (artifact / "pass_03" / "result.json").exists()


@pytest.mark.asyncio
async def test_partial_resume_after_synthesizer(tmp_path: Path) -> None:
    """Pre-populate all 3 role outputs + synth_meta → only judges run.

    Verifies: SYNTHESIZER coin-flip is NOT re-rolled (the recorded synth_meta
    is preserved on disk; judge prompts use the X/Y identity recorded there
    via the on-disk version_ab.md).
    """
    artifact = _make_artifact_dir(tmp_path, "partial-synth")
    initial = "MARK_A_ONLY_INITIAL"
    inc2 = _setup_two_b_wins(artifact, initial)

    pre_synth = {"x_label": "B", "y_label": "A"}
    _write_version_a(artifact, 3, inc2)
    _write_critic(artifact, 3, "PARTIAL_CRITIC")
    _write_version_b(artifact, 3, "PARTIAL_MARK_B_ONLY")
    _write_version_ab(artifact, 3, "PARTIAL_MARK_AB_ONLY")
    _write_synth_meta(artifact, 3, pre_synth["x_label"], pre_synth["y_label"])

    cfg = TournamentConfig(num_judges=3, convergence_k=1, max_rounds=4)
    client = StubLLMClient(fn=_favor_label_factory("A"))
    t = Tournament(
        handler=_handler(),
        client=client,
        cfg=cfg,
        artifact_dir=artifact,
        rng=random.Random(0),
    )
    await t.run("Task.", initial)

    counts: dict[str, int] = {}
    for c in client.calls:
        counts[c["role"]] = counts.get(c["role"], 0) + 1

    assert counts.get("critic_t", 0) == 0
    assert counts.get("architect_b", 0) == 0
    assert counts.get("synthesizer", 0) == 0
    assert counts.get("judge", 0) >= 3

    # synth_meta on disk is unchanged — coin-flip was not re-rolled.
    on_disk_synth = json.loads(
        (artifact / "pass_03" / "synth_meta.json").read_text(encoding="utf-8")
    )
    assert on_disk_synth == pre_synth
    assert (artifact / "pass_03" / "result.json").exists()


@pytest.mark.asyncio
async def test_partial_resume_with_two_judges_done(tmp_path: Path) -> None:
    """Pre-populate 2 judge orders + responses → only judge 2 fires; Borda over 3."""
    artifact = _make_artifact_dir(tmp_path, "partial-two-judges")
    initial = "MARK_A_ONLY_INITIAL"
    inc2 = _setup_two_b_wins(artifact, initial)

    # Pre-populate complete role outputs for pass 3.
    _write_version_a(artifact, 3, inc2)
    _write_critic(artifact, 3, "PARTIAL_CRITIC")
    _write_version_b(artifact, 3, "PARTIAL_MARK_B_ONLY")
    _write_version_ab(artifact, 3, "PARTIAL_MARK_AB_ONLY")
    _write_synth_meta(artifact, 3, "A", "B")

    # Pre-populate judges 0 and 1 (orders + responses). Judge 2 missing.
    # Order maps slot (1/2/3) → canonical label. The recorded ranking is the
    # slot-index list (e.g. ["1","2","3"] means slot-1 first).
    judge0_order = {1: "A", 2: "B", 3: "AB"}
    judge1_order = {1: "AB", 2: "A", 3: "B"}
    _write_judge_order(artifact, 3, 0, judge0_order)
    _write_judge_response(
        artifact,
        3,
        0,
        {"raw": "RANKING: 1, 2, 3", "ranking": ["1", "2", "3"], "error": None},
    )
    _write_judge_order(artifact, 3, 1, judge1_order)
    _write_judge_response(
        artifact,
        3,
        1,
        {"raw": "RANKING: 2, 3, 1", "ranking": ["2", "3", "1"], "error": None},
    )

    cfg = TournamentConfig(num_judges=3, convergence_k=1, max_rounds=4)
    # Use favor "B" so the fresh judge call can find a MARK_B_ONLY prefix in
    # the incumbent (which is "MARK_B_ONLY_02") — otherwise the stub raises.
    client = StubLLMClient(fn=_favor_label_factory("B"))
    t = Tournament(
        handler=_handler(),
        client=client,
        cfg=cfg,
        artifact_dir=artifact,
        rng=random.Random(0),
    )
    await t.run("Task.", initial)

    # Only judge 2 makes an LLM call; judges 0 and 1 are reused from disk.
    judge_calls = [c for c in client.calls if c["role"] == "judge"]
    assert len(judge_calls) == 1, (
        f"expected exactly 1 judge LLM call (only judge 2); got {len(judge_calls)}"
    )

    # Final result reflects all 3 rankings (Borda over 3 valid judges).
    result = json.loads(
        (artifact / "pass_03" / "result.json").read_text(encoding="utf-8")
    )
    assert result["valid_judges"] == 3, (
        f"expected 3 valid judges (2 reused + 1 fresh); got {result['valid_judges']}"
    )
    assert len(result["judge_details"]) == 3


@pytest.mark.asyncio
async def test_partial_resume_with_one_judge_order_no_response(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pre-populate judges/2_order.json only → judge 2 reuses recorded order.

    Mid-LLM-call kill scenario: order is on disk but response isn't. On
    resume, judge 2 must re-run with the recorded order (no re-shuffle), and
    a ``tournament.judge_order_reused`` log must fire.
    """
    artifact = _make_artifact_dir(tmp_path, "partial-one-order")
    initial = "MARK_A_ONLY_INITIAL"
    inc2 = _setup_two_b_wins(artifact, initial)

    _write_version_a(artifact, 3, inc2)
    _write_critic(artifact, 3, "PARTIAL_CRITIC")
    _write_version_b(artifact, 3, "PARTIAL_MARK_B_ONLY")
    _write_version_ab(artifact, 3, "PARTIAL_MARK_AB_ONLY")
    _write_synth_meta(artifact, 3, "A", "B")

    # Pre-populate ONLY judge 2's order (no response). Pinned shape so we
    # can verify it's preserved verbatim.
    pinned_order = {1: "AB", 2: "A", 3: "B"}
    _write_judge_order(artifact, 3, 2, pinned_order)

    cfg = TournamentConfig(num_judges=3, convergence_k=1, max_rounds=4)
    # Use favor "B" so the stub's prefix matcher finds MARK_B_ONLY in the
    # incumbent or in version_b ("PARTIAL_MARK_B_ONLY"); avoids stub asserts.
    client = StubLLMClient(fn=_favor_label_factory("B"))
    t = Tournament(
        handler=_handler(),
        client=client,
        cfg=cfg,
        artifact_dir=artifact,
        rng=random.Random(0),
    )
    await t.run("Task.", initial)

    # All 3 judges make LLM calls (judges 0/1 fresh; judge 2 with recorded order).
    judge_calls = [c for c in client.calls if c["role"] == "judge"]
    assert len(judge_calls) == 3

    # Judge 2's order on disk is unchanged.
    on_disk = json.loads(
        (artifact / "pass_03" / "judges" / "2_order.json").read_text(encoding="utf-8")
    )
    assert on_disk == {str(k): v for k, v in pinned_order.items()}, (
        f"judge 2's order was overwritten: {on_disk}"
    )

    # Judge 2's response is now persisted.
    assert (artifact / "pass_03" / "judges" / "2_response.json").exists()

    # tournament.judge_order_reused log emitted.
    out = capsys.readouterr().out
    assert "tournament.judge_order_reused" in out, (
        f"expected 'tournament.judge_order_reused' in stdout; got: {out[-2000:]}"
    )


@pytest.mark.asyncio
async def test_partial_resume_stale_version_a_starts_fresh(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Stale version_a.md → partial discarded; CRITIC re-runs.

    If the on-disk version_a hash for the in-progress pass doesn't match the
    recovered incumbent, downstream artifacts (critic, version_b, ...) are
    treated as stale. Tier 3 stale-guard logs ``tournament.partial_resume_stale``
    and discards the partial.
    """
    artifact = _make_artifact_dir(tmp_path, "partial-stale")
    initial = "MARK_A_ONLY_INITIAL"
    inc2 = _setup_two_b_wins(artifact, initial)

    # Pre-populate pass 3's version_a with content that does NOT match the
    # recovered incumbent (inc2). This trips the stale-guard.
    stale_va = "STALE_VERSION_A_FROM_AN_EARLIER_RUN"
    assert stale_va != inc2  # sanity
    _write_version_a(artifact, 3, stale_va)
    _write_critic(artifact, 3, "STALE_CRITIC_SHOULD_BE_DISCARDED")

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

    out = capsys.readouterr().out
    assert "tournament.partial_resume_stale" in out, (
        f"expected 'tournament.partial_resume_stale' in stdout; got: {out[-2000:]}"
    )

    # CRITIC re-ran (partial was discarded).
    counts = {c["role"]: 0 for c in client.calls}
    for c in client.calls:
        counts[c["role"]] = counts.get(c["role"], 0) + 1
    assert counts.get("critic_t", 0) >= 1, (
        f"CRITIC should re-run after stale guard; counts={counts}"
    )


@pytest.mark.asyncio
async def test_synth_meta_round_trip(tmp_path: Path) -> None:
    """Run a full pass; verify synth_meta.json shape; resume next pass with it.

    Phase 1: run a full tournament and assert pass_01/synth_meta.json contains
    ``x_label`` and ``y_label`` ∈ {"A", "B"}.
    Phase 2: pre-populate pass 2 with all role outputs (incl. synth_meta) +
    the actual incumbent_after_01.md from phase 1; resume; assert SYNTHESIZER
    is not called.
    """
    artifact = _make_artifact_dir(tmp_path, "synth-roundtrip")
    initial = "MARK_A_ONLY_INITIAL"

    # Phase 1: fresh run, single pass that B-wins (judge prefers B).
    cfg = TournamentConfig(num_judges=1, convergence_k=1, max_rounds=1)
    client1 = StubLLMClient(fn=_favor_label_factory("B"))
    t1 = Tournament(
        handler=_handler(),
        client=client1,
        cfg=cfg,
        artifact_dir=artifact,
        rng=random.Random(0),
    )
    await t1.run("Task.", initial)

    synth_meta_path = artifact / "pass_01" / "synth_meta.json"
    assert synth_meta_path.exists(), "synth_meta.json must be written after pass"
    on_disk = json.loads(synth_meta_path.read_text(encoding="utf-8"))
    assert set(on_disk.keys()) == {"x_label", "y_label"}, (
        f"synth_meta keys: {sorted(on_disk.keys())}"
    )
    assert on_disk["x_label"] in {"A", "B"}
    assert on_disk["y_label"] in {"A", "B"}
    assert on_disk["x_label"] != on_disk["y_label"]

    # Phase 2: extend with a new partial pass 2.
    inc1_path = artifact / "incumbent_after_01.md"
    assert inc1_path.exists(), "B-win must have produced incumbent_after_01.md"
    inc1 = inc1_path.read_text(encoding="utf-8")

    # Pre-populate pass 2 with all role outputs + synth_meta.
    pre_synth = {"x_label": "A", "y_label": "B"}
    _write_version_a(artifact, 2, inc1)
    _write_critic(artifact, 2, "P2_CRITIC")
    _write_version_b(artifact, 2, "P2_MARK_B_ONLY")
    _write_version_ab(artifact, 2, "P2_MARK_AB_ONLY")
    _write_synth_meta(artifact, 2, pre_synth["x_label"], pre_synth["y_label"])

    cfg2 = TournamentConfig(num_judges=1, convergence_k=1, max_rounds=2)
    client2 = StubLLMClient(fn=_favor_label_factory("A"))
    t2 = Tournament(
        handler=_handler(),
        client=client2,
        cfg=cfg2,
        artifact_dir=artifact,
        rng=random.Random(0),
    )
    await t2.run("Task.", initial)

    synth_calls = [c for c in client2.calls if c["role"] == "synthesizer"]
    assert synth_calls == [], (
        f"SYNTHESIZER must not run when pass 2 has version_ab.md + synth_meta.json; "
        f"got {len(synth_calls)} calls"
    )


@pytest.mark.asyncio
async def test_partial_resume_diverges_in_later_passes(tmp_path: Path) -> None:
    """Document the determinism caveat: partial resume diverges at pass N+1.

    The Tier 3 plan documents that skipped roles do NOT draw from the RNG, so
    after a partial-resume pass, the RNG state at the start of the next pass
    differs from a fresh-only run. This locks that boundary in: pass 2 is
    identical (same incumbent), but pass 3's judge orders differ between
    fresh and resumed runs.
    """
    cfg = TournamentConfig(num_judges=3, convergence_k=2, max_rounds=4)
    initial = "MARK_A_ONLY_INITIAL"

    # ── Fresh baseline run ──
    fresh_dir = tmp_path / "fresh"
    fresh_dir.mkdir()
    fresh_client = StubLLMClient(fn=_favor_label_factory("B"))
    t_fresh = Tournament(
        handler=_handler(),
        client=fresh_client,
        cfg=cfg,
        artifact_dir=fresh_dir,
        rng=random.Random(7),
    )
    await t_fresh.run("Task.", initial)

    # Capture fresh pass 2 + pass 3 results.
    fresh_p2 = json.loads(
        (fresh_dir / "pass_02" / "result.json").read_text(encoding="utf-8")
    )
    fresh_p3_path = fresh_dir / "pass_03" / "result.json"
    if not fresh_p3_path.exists():
        pytest.skip(
            "fresh run converged before pass 3; cannot compare divergence"
        )
    fresh_p3 = json.loads(fresh_p3_path.read_text(encoding="utf-8"))
    fresh_p3_orders = [
        d.get("order") for d in fresh_p3["judge_details"] if "order" in d
    ]

    # ── Resumed run: same setup, but pre-populate pass 2 partial state ──
    resumed_dir = tmp_path / "resumed"
    resumed_dir.mkdir()
    (resumed_dir / "initial_a.md").write_text(initial, encoding="utf-8")

    # Replay fresh pass 1 verbatim into resumed_dir. We need pass 1 complete
    # and pass 2 partial (with all role outputs except CRITIC missing —
    # actually populate ALL roles to skip everything except CRITIC, so the
    # RNG asymmetry shows up).
    fresh_p1_dir = fresh_dir / "pass_01"
    resumed_p1_dir = resumed_dir / "pass_01"
    resumed_p1_dir.mkdir()
    for name in ("version_a.md", "critic.md", "version_b.md", "version_ab.md", "result.json"):
        src = fresh_p1_dir / name
        if src.exists():
            (resumed_p1_dir / name).write_text(
                src.read_text(encoding="utf-8"), encoding="utf-8"
            )
    # Replay any synth_meta and judges/ from fresh pass 1.
    if (fresh_p1_dir / "synth_meta.json").exists():
        (resumed_p1_dir / "synth_meta.json").write_text(
            (fresh_p1_dir / "synth_meta.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    fresh_p1_judges = fresh_p1_dir / "judges"
    if fresh_p1_judges.exists():
        (resumed_p1_dir / "judges").mkdir()
        for jf in fresh_p1_judges.iterdir():
            (resumed_p1_dir / "judges" / jf.name).write_text(
                jf.read_text(encoding="utf-8"), encoding="utf-8"
            )
    # Replay incumbent_after_01.md if present.
    if (fresh_dir / "incumbent_after_01.md").exists():
        (resumed_dir / "incumbent_after_01.md").write_text(
            (fresh_dir / "incumbent_after_01.md").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    # Replay fresh pass 2's role outputs (NOT result.json) into resumed pass 2,
    # but skip critic.md so CRITIC re-runs while everything else (synth +
    # judges) is reused from disk. Pass-2 result must be identical because
    # Borda runs over the recorded judge data; pass-3 will diverge because
    # the resumed run's RNG didn't advance during pass 2 (skipped roles
    # don't draw from RNG).
    fresh_p2_dir = fresh_dir / "pass_02"
    resumed_p2_dir = resumed_dir / "pass_02"
    resumed_p2_dir.mkdir()
    for name in ("version_a.md", "version_b.md", "version_ab.md"):
        src = fresh_p2_dir / name
        if src.exists():
            (resumed_p2_dir / name).write_text(
                src.read_text(encoding="utf-8"), encoding="utf-8"
            )
    if (fresh_p2_dir / "synth_meta.json").exists():
        (resumed_p2_dir / "synth_meta.json").write_text(
            (fresh_p2_dir / "synth_meta.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    # Replay all judge orders + responses so the recorded-Borda path runs
    # (judges are not re-called).
    fresh_p2_judges = fresh_p2_dir / "judges"
    if fresh_p2_judges.exists():
        (resumed_p2_dir / "judges").mkdir()
        for jf in fresh_p2_judges.iterdir():
            (resumed_p2_dir / "judges" / jf.name).write_text(
                jf.read_text(encoding="utf-8"), encoding="utf-8"
            )

    resumed_client = StubLLMClient(fn=_favor_label_factory("B"))
    t_resumed = Tournament(
        handler=_handler(),
        client=resumed_client,
        cfg=cfg,
        artifact_dir=resumed_dir,
        rng=random.Random(7),
    )
    await t_resumed.run("Task.", initial)

    # Pass 2 result is identical (same incumbent — we replayed all role
    # outputs except critic, but Borda over the recorded judge data is
    # deterministic).
    resumed_p2 = json.loads(
        (resumed_dir / "pass_02" / "result.json").read_text(encoding="utf-8")
    )
    assert resumed_p2["winner"] == fresh_p2["winner"], (
        "pass 2 winner must match between fresh and resumed runs"
    )
    assert (
        resumed_p2["incumbent_hash_after"] == fresh_p2["incumbent_hash_after"]
    ), "pass 2 incumbent hash must match between fresh and resumed runs"

    # Pass 3 may diverge: in resumed run, RNG didn't advance during pass 2
    # (synth + judges skipped), so pass 3 draws different values.
    resumed_p3_path = resumed_dir / "pass_03" / "result.json"
    if not resumed_p3_path.exists():
        # Resumed converged before pass 3 — that itself is divergence.
        return
    resumed_p3 = json.loads(resumed_p3_path.read_text(encoding="utf-8"))
    resumed_p3_orders = [
        d.get("order") for d in resumed_p3["judge_details"] if "order" in d
    ]

    # Either incumbent or judge orders differ.
    incumbent_diff = (
        resumed_p3["incumbent_hash_after"] != fresh_p3["incumbent_hash_after"]
    )
    orders_diff = resumed_p3_orders != fresh_p3_orders
    assert incumbent_diff or orders_diff, (
        f"expected pass 3 incumbent OR judge orders to differ between fresh and "
        f"resumed runs (RNG asymmetry caveat); fresh_p3_orders={fresh_p3_orders}, "
        f"resumed_p3_orders={resumed_p3_orders}, "
        f"fresh_inc={fresh_p3['incumbent_hash_after']}, "
        f"resumed_inc={resumed_p3['incumbent_hash_after']}"
    )


@pytest.mark.asyncio
async def test_partial_resume_logging_lists_skipped_roles(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """tournament.partial_pass_resume log lists the canonical skipped role names."""
    artifact = _make_artifact_dir(tmp_path, "partial-log")
    initial = "MARK_A_ONLY_INITIAL"
    inc2 = _setup_two_b_wins(artifact, initial)

    _write_version_a(artifact, 3, inc2)
    _write_critic(artifact, 3, "PARTIAL_CRITIC")
    _write_version_b(artifact, 3, "PARTIAL_MARK_B_ONLY")

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

    out = capsys.readouterr().out
    # Find the partial_pass_resume event line and verify roles_skipped.
    matched_lines = [
        line for line in out.splitlines() if "tournament.partial_pass_resume" in line
    ]
    assert matched_lines, (
        f"expected 'tournament.partial_pass_resume' in stdout; got: {out[-2000:]}"
    )
    line = matched_lines[0]
    # Production code uses canonical role names: "critic_t", "architect_b".
    assert "critic_t" in line, f"missing 'critic_t' in log line: {line}"
    assert "architect_b" in line, f"missing 'architect_b' in log line: {line}"
