"""End-to-end tests for Tournament.run with a string-content handler.

Uses StubLLMClient to drive the tournament offline with scripted responses.
Verifies:
    - convergence at streak >= convergence_k
    - artifacts written (initial_a, incumbent_after_NN, pass_NN/*, final, history)
    - hashes change iff incumbent changes
    - deterministic rng produces byte-identical artifacts on re-run
"""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Callable

import pytest

from tournament import (
    ContentHandler,
    PassResult,
    StubLLMClient,
    Tournament,
    TournamentConfig,
)
from tournament.prompts import JUDGE_RANK_3_PROMPT


# ── Test handler: T = str (plain markdown passthrough) ─────────────────


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
        # Mimic autoreason: render proposals in the shuffled order.
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


# ── Judge-response helpers ────────────────────────────────────────────────


def _judge_response_favoring(label: str, order: dict[int, str]) -> str:
    """Given the handler's randomized `order`, return a judge text whose
    RANKING places `label` first.

    However the tournament builds `order` internally (via its rng) — tests
    don't see that directly. For scripted tests we use a callback-based
    StubLLMClient that inspects the prompt and picks the right ranking.
    """
    # Find which position the target label occupies.
    pos = next(i for i, lbl in order.items() if lbl == label)
    others = [i for i in (1, 2, 3) if i != pos]
    # Order: [target, other1, other2]
    return f"Deliberating...\n\nRANKING: {pos}, {others[0]}, {others[1]}"


def _judge_callback_always_A(role: str, system: str, user: str) -> str:
    """Inspect the rendered judge prompt, find which PROPOSAL slot contains
    the incumbent A text, and emit a ranking that puts it first.

    The prompt has lines like:
        PROPOSAL 1:
        ---
        <text>
        ---
    We look for the incumbent marker embedded in the handler's initial text.
    """
    if role != "judge":
        return _role_defaults(role, user)
    # Scan for each PROPOSAL block's content marker (we know A's text starts
    # with "MARK_A_ONLY" or "MARK_AB_ONLY_" etc in the scripted scenario).
    return _judge_prefer(user, prefer_prefix="MARK_A_ONLY")


def _judge_prefer(prompt_text: str, prefer_prefix: str) -> str:
    """Emit a RANKING placing the proposal containing `prefer_prefix` first.

    The judge prompt has lines like::

        PROPOSAL 1:
        ---
        <body>
        ---

    We slice each PROPOSAL 1/2/3 block tightly (from its marker up to the
    next PROPOSAL marker or end-of-string) so marker searches can't bleed
    between blocks when bodies are short.
    """
    # Locate all PROPOSAL marker offsets.
    offsets: dict[int, int] = {}
    for slot in (1, 2, 3):
        marker = f"PROPOSAL {slot}:"
        idx = prompt_text.find(marker)
        if idx >= 0:
            offsets[slot] = idx

    # Sort by offset to determine each block's end.
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

    assert preferred_slot is not None, (
        f"could not find {prefer_prefix!r} in any PROPOSAL body: {prompt_text[:400]}"
    )
    others = [s for s in (1, 2, 3) if s != preferred_slot]
    return f"RANKING: {preferred_slot}, {others[0]}, {others[1]}"


def _role_defaults(role: str, user: str) -> str:
    """Non-judge scripted responses keyed by role.

    Important: the marker strings must be mutually non-prefix so that
    `_judge_prefer` matches exactly the intended label.
    """
    if role == "critic_t":
        return "CRITIC: The proposal is flawed."
    if role == "architect_b":
        return "MARK_B_ONLY"
    if role == "synthesizer":
        return "MARK_AB_ONLY"
    raise AssertionError(f"unexpected role: {role}")


def _favor_label_factory(
    label: str, prefix_map: dict[str, str]
) -> Callable[[str, str, str], str]:
    """Build a callback that always picks `label` for judge calls."""

    def _cb(role: str, system: str, user: str) -> str:
        if role != "judge":
            return _role_defaults(role, user)
        return _judge_prefer(user, prefix_map[label])

    return _cb


def _prefix_map() -> dict[str, str]:
    return {
        "A": "MARK_A_ONLY",
        "B": "MARK_B_ONLY",
        "AB": "MARK_AB_ONLY",
    }


# ── Scenarios ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_convergence_all_A_in_two_passes(tmp_path: Path) -> None:
    """When judges always favor A, the tournament converges at pass 2 (k=2)."""
    cfg = TournamentConfig(num_judges=3, convergence_k=2, max_rounds=10)
    client = StubLLMClient(fn=_favor_label_factory("A", _prefix_map()))
    artifact = tmp_path / "tournaments" / "test-run"

    t = Tournament(
        handler=_handler(),
        client=client,
        cfg=cfg,
        artifact_dir=artifact,
        rng=random.Random(0),
    )
    final, history = await t.run(
        task_prompt="Write a plan.",
        initial="MARK_A_ONLY_INITIAL",
    )

    assert final == "MARK_A_ONLY_INITIAL"
    assert len(history) == 2
    assert all(h.winner == "A" for h in history)

    # Artifacts on disk.
    assert (artifact / "initial_a.md").read_text() == "MARK_A_ONLY_INITIAL"
    assert (artifact / "final_output.md").read_text() == "MARK_A_ONLY_INITIAL"
    hist = json.loads((artifact / "history.json").read_text())
    assert len(hist) == 2
    assert hist[0]["winner"] == "A"

    # No incumbent_after_NN files because winner was always A.
    incumbents = sorted(artifact.glob("incumbent_after_*.md"))
    assert incumbents == []

    # pass_01 and pass_02 dirs exist with expected files.
    for n in (1, 2):
        p = artifact / f"pass_{n:02d}"
        assert (p / "version_a.md").exists()
        assert (p / "critic.md").exists()
        assert (p / "version_b.md").exists()
        assert (p / "version_ab.md").exists()
        res = json.loads((p / "result.json").read_text())
        assert res["winner"] == "A"
        assert res["pass_num"] == n


@pytest.mark.asyncio
async def test_ab_always_wins_hits_cap(tmp_path: Path) -> None:
    """When judges always favor AB AND content changes each pass, no streak forms.

    The synthesizer must produce a different output each pass to genuinely
    represent a divergent (non-converging) scenario; otherwise Fix 1's
    no-change short-circuit (hash equality) will rescue the loop.
    """
    cfg = TournamentConfig(num_judges=3, convergence_k=2, max_rounds=3)

    prefix_map = _prefix_map()

    def _cb(role: str, system: str, user: str) -> str:
        if role == "synthesizer":
            n = getattr(_cb, "_n", 0) + 1
            _cb._n = n  # type: ignore[attr-defined]
            return f"MARK_AB_ONLY_PASS{n}"  # different each pass → AB by hash too
        if role != "judge":
            return _role_defaults(role, user)
        return _judge_prefer(user, prefix_map["AB"])

    client = StubLLMClient(fn=_cb)
    artifact = tmp_path / "tournaments" / "ab-wins"

    t = Tournament(
        handler=_handler(),
        client=client,
        cfg=cfg,
        artifact_dir=artifact,
        rng=random.Random(1),
    )
    final, history = await t.run(
        task_prompt="Task.",
        initial="MARK_A_ONLY_INITIAL",
    )

    assert final == "MARK_AB_ONLY_PASS3"
    assert len(history) == cfg.max_rounds
    assert all(h.winner == "AB" for h in history)

    # Each non-A pass should have produced an incumbent_after_NN.md.
    incs = sorted(artifact.glob("incumbent_after_*.md"))
    assert len(incs) == 3
    # Each pass writes a distinct AB content.
    assert [p.read_text() for p in incs] == [
        "MARK_AB_ONLY_PASS1",
        "MARK_AB_ONLY_PASS2",
        "MARK_AB_ONLY_PASS3",
    ]


@pytest.mark.asyncio
async def test_alternating_then_settles(tmp_path: Path) -> None:
    """Alternate B, AB, then A, A should converge at pass 4 (streak=2 at k=2)."""
    cfg = TournamentConfig(num_judges=3, convergence_k=2, max_rounds=10)

    # Sequence: pass1→B, pass2→AB, pass3→A, pass4→A (converge here).
    labels = ["B", "AB", "A", "A", "A"]
    prefix_map = _prefix_map()

    def _cb(role: str, system: str, user: str) -> str:
        if role != "judge":
            return _role_defaults(role, user)
        # Determine which pass this judge is for by counting how many
        # author_b responses we've observed so far. `StubLLMClient` increments
        # per-role counts but doesn't expose them here. Instead we key on the
        # incumbent prefix present in the PROPOSAL 1/2/3 block.
        # A simpler trick: the incumbent text is whatever label "A" currently
        # maps to in the tournament. For the scripted order we pick the
        # label in `labels` that hasn't been consumed yet.
        idx = getattr(_cb, "_call_num", 0)
        # Three judges per pass, so pass number = idx // 3.
        pass_num = idx // 3
        target = labels[pass_num]
        _cb._call_num = idx + 1  # type: ignore[attr-defined]
        return _judge_prefer(user, prefix_map[target])

    client = StubLLMClient(fn=_cb)
    artifact = tmp_path / "tournaments" / "alternating"

    t = Tournament(
        handler=_handler(),
        client=client,
        cfg=cfg,
        artifact_dir=artifact,
        rng=random.Random(2),
    )
    final, history = await t.run(
        task_prompt="Task.",
        initial="MARK_A_ONLY_INITIAL",
    )

    assert [h.winner for h in history] == ["B", "AB", "A", "A"]
    assert len(history) == 4

    # Final should be the incumbent after pass 3 (an A win kept pass-2's AB);
    # wait — pass 3's A win means streak=1 and incumbent stays AB; pass 4's A
    # makes streak=2 → converge. So final == MARK_AB_ONLY.
    assert final == "MARK_AB_ONLY"


@pytest.mark.asyncio
async def test_hash_changes_only_on_incumbent_change(tmp_path: Path) -> None:
    """incumbent_hash_before/after should differ iff the incumbent changed."""
    cfg = TournamentConfig(num_judges=1, convergence_k=2, max_rounds=2)
    # Pass 1: AB wins (incumbent changes). Pass 2: A wins (incumbent stable).
    labels = ["AB", "A"]
    prefix_map = _prefix_map()

    def _cb(role: str, system: str, user: str) -> str:
        if role != "judge":
            return _role_defaults(role, user)
        idx = getattr(_cb, "_n", 0)
        _cb._n = idx + 1  # type: ignore[attr-defined]
        target = labels[idx]
        return _judge_prefer(user, prefix_map[target])

    client = StubLLMClient(fn=_cb)
    artifact = tmp_path / "t" / "hashtest"
    t = Tournament(
        handler=_handler(),
        client=client,
        cfg=cfg,
        artifact_dir=artifact,
        rng=random.Random(3),
    )
    _, history = await t.run("Task.", "MARK_A_ONLY_INITIAL")
    assert len(history) == 2

    # Pass 1: AB wins → hash differs.
    assert history[0].winner == "AB"
    assert history[0].incumbent_hash_before != history[0].incumbent_hash_after

    # Pass 2: A wins → hash stable.
    assert history[1].winner == "A"
    assert history[1].incumbent_hash_before == history[1].incumbent_hash_after

    # The after-hash of pass 1 should equal the before-hash of pass 2.
    assert history[0].incumbent_hash_after == history[1].incumbent_hash_before


@pytest.mark.asyncio
async def test_history_json_shape(tmp_path: Path) -> None:
    """history.json is a list of dicts matching PassResult.model_dump()."""
    cfg = TournamentConfig(num_judges=1, convergence_k=1, max_rounds=1)
    client = StubLLMClient(fn=_favor_label_factory("A", _prefix_map()))
    artifact = tmp_path / "t" / "shape"
    t = Tournament(
        handler=_handler(),
        client=client,
        cfg=cfg,
        artifact_dir=artifact,
        rng=random.Random(0),
    )
    _, history = await t.run("Task.", "MARK_A_ONLY_INITIAL")

    raw = json.loads((artifact / "history.json").read_text())
    assert isinstance(raw, list)
    assert len(raw) == len(history) == 1
    entry = raw[0]
    required = {
        "pass_num",
        "winner",
        "scores",
        "valid_judges",
        "elapsed_s",
        "judge_details",
        "incumbent_hash_before",
        "incumbent_hash_after",
        "meta",
    }
    assert required.issubset(entry.keys())


@pytest.mark.asyncio
async def test_deterministic_artifacts_with_same_seed(tmp_path: Path) -> None:
    """Same rng seed + same stub responses → byte-identical per-pass .md files.

    (result.json differs only by meta.timestamp, which is stripped here.)
    """
    cfg = TournamentConfig(num_judges=3, convergence_k=2, max_rounds=3)
    prefix_map = _prefix_map()

    async def _run_once(dir_: Path) -> None:
        client = StubLLMClient(fn=_favor_label_factory("A", prefix_map))
        t = Tournament(
            handler=_handler(),
            client=client,
            cfg=cfg,
            artifact_dir=dir_,
            rng=random.Random(12345),
        )
        await t.run("Task.", "MARK_A_ONLY_INITIAL")

    a = tmp_path / "run_a"
    b = tmp_path / "run_b"
    await _run_once(a)
    await _run_once(b)

    # Markdown artifacts must match byte-for-byte.
    for name in ("initial_a.md", "final_output.md"):
        assert (a / name).read_bytes() == (b / name).read_bytes()
    for p in (1, 2):
        for name in ("version_a.md", "critic.md", "version_b.md", "version_ab.md"):
            assert (a / f"pass_{p:02d}" / name).read_bytes() == (
                b / f"pass_{p:02d}" / name
            ).read_bytes()

    # result.json should be identical except for the meta.timestamp field.
    for p in (1, 2):
        ra = json.loads((a / f"pass_{p:02d}" / "result.json").read_text())
        rb = json.loads((b / f"pass_{p:02d}" / "result.json").read_text())
        ra.pop("meta", None)
        rb.pop("meta", None)
        # elapsed_s is wall-clock — strip it for comparison too.
        ra.pop("elapsed_s", None)
        rb.pop("elapsed_s", None)
        assert ra == rb


@pytest.mark.asyncio
async def test_artifact_path_layout(tmp_path: Path) -> None:
    """Sanity check on the standardized path layout."""
    cfg = TournamentConfig(num_judges=1, convergence_k=2, max_rounds=3)
    client = StubLLMClient(fn=_favor_label_factory("A", _prefix_map()))
    artifact = tmp_path / "tournaments" / "test-run"
    t = Tournament(
        handler=_handler(),
        client=client,
        cfg=cfg,
        artifact_dir=artifact,
        rng=random.Random(0),
    )
    await t.run("Task.", "MARK_A_ONLY_INITIAL")

    assert artifact.is_dir()
    assert (artifact / "initial_a.md").is_file()
    assert (artifact / "final_output.md").is_file()
    assert (artifact / "history.json").is_file()


@pytest.mark.asyncio
async def test_judge_parse_failure_counts_invalid(tmp_path: Path) -> None:
    """If a judge returns unparseable text, valid_judges decreases."""
    cfg = TournamentConfig(num_judges=3, convergence_k=1, max_rounds=1)

    judge_call = [0]

    def _cb(role: str, system: str, user: str) -> str:
        if role != "judge":
            return _role_defaults(role, user)
        # First two judges fail to produce RANKING, third votes A.
        judge_call[0] += 1
        if judge_call[0] < 3:
            return "(I cannot rank these proposals.)"
        return _judge_prefer(user, "MARK_A_ONLY")

    client = StubLLMClient(fn=_cb)
    artifact = tmp_path / "t" / "parsefail"
    t = Tournament(
        handler=_handler(),
        client=client,
        cfg=cfg,
        artifact_dir=artifact,
        rng=random.Random(0),
    )
    _, history = await t.run("Task.", "MARK_A_ONLY_INITIAL")

    assert len(history) == 1
    assert history[0].valid_judges == 1
    assert history[0].winner == "A"


@pytest.mark.asyncio
async def test_call_counts_per_pass(tmp_path: Path) -> None:
    """Each pass should make: 1 critic + 1 author_b + 1 synth + N judge calls."""
    cfg = TournamentConfig(num_judges=3, convergence_k=2, max_rounds=2)
    client = StubLLMClient(fn=_favor_label_factory("A", _prefix_map()))
    artifact = tmp_path / "t" / "counts"
    t = Tournament(
        handler=_handler(),
        client=client,
        cfg=cfg,
        artifact_dir=artifact,
        rng=random.Random(0),
    )
    await t.run("Task.", "MARK_A_ONLY_INITIAL")

    roles = [c["role"] for c in client.calls]
    # 2 passes × (1 critic + 1 author_b + 1 synth + 3 judges) = 12 calls
    assert roles.count("critic_t") == 2
    assert roles.count("architect_b") == 2
    assert roles.count("synthesizer") == 2
    assert roles.count("judge") == 6
    assert len(roles) == 12


def test_pass_result_is_pydantic() -> None:
    # Sanity test: PassResult serializes round-trip cleanly.
    r = PassResult(
        pass_num=1,
        winner="A",
        scores={"A": 3, "B": 2, "AB": 1},
        valid_judges=1,
        elapsed_s=0.5,
        judge_details=[{"ranking": ["A", "B", "AB"]}],
        incumbent_hash_before="abc",
        incumbent_hash_after="abc",
    )
    dumped = r.model_dump(mode="json")
    assert dumped["winner"] == "A"
    assert "meta" in dumped
    r2 = PassResult.model_validate(dumped)
    assert r2.pass_num == 1


@pytest.mark.asyncio
async def test_checkpoint_timing_writes_artifacts_incrementally(tmp_path: Path) -> None:
    """Tier 3: per-role artifacts are written as each role completes, not in a
    single end-of-pass batch.

    Asserts the timing by snapshotting the contents of ``pass_01/`` at the
    *entry* of each LLM call. Because every per-role write happens BEFORE the
    next role's LLM call, the snapshot taken when role ``R`` is invoked
    reflects all writes performed by all roles that completed before ``R``.
    """

    class _SnapshotClient(StubLLMClient):
        def __init__(
            self,
            *args,
            snapshot_dir: Path,
            **kwargs,
        ) -> None:
            super().__init__(*args, **kwargs)
            self.snapshots: list[dict] = []
            self._snapshot_dir = snapshot_dir

        async def call(
            self,
            *,
            system: str,
            user: str,
            role: str,
            model: str | None = None,
        ) -> str:
            # Snapshot BEFORE the LLM call. Reflects on-disk state at this
            # role's entry point.
            files: set[str] = set()
            if self._snapshot_dir.exists():
                for p in self._snapshot_dir.rglob("*"):
                    if p.is_file():
                        files.add(p.relative_to(self._snapshot_dir).as_posix())
            n_for_role = self._role_counts.get(role, 0) + 1
            self.snapshots.append({"role": role, "n": n_for_role, "files": files})
            return await super().call(
                system=system, user=user, role=role, model=model
            )

    cfg = TournamentConfig(num_judges=3, convergence_k=1, max_rounds=1)
    artifact = tmp_path / "tournaments" / "checkpoint-timing"
    pass1_dir = artifact / "pass_01"

    client = _SnapshotClient(
        fn=_favor_label_factory("A", _prefix_map()),
        snapshot_dir=pass1_dir,
    )
    t = Tournament(
        handler=_handler(),
        client=client,
        cfg=cfg,
        artifact_dir=artifact,
        rng=random.Random(42),
    )
    await t.run("Task.", "MARK_A_ONLY_INITIAL")

    # ── Sequence checks: the first three calls are CRITIC → ARCHITECT_B →
    # SYNTHESIZER, in that strict order. ────────────────────────────────────
    assert len(client.snapshots) == 6  # critic + arch_b + synth + 3 judges
    assert client.snapshots[0]["role"] == "critic_t"
    assert client.snapshots[1]["role"] == "architect_b"
    assert client.snapshots[2]["role"] == "synthesizer"
    assert {s["role"] for s in client.snapshots[3:6]} == {"judge"}

    # ── Snapshot 0 (CRITIC entry): version_a.md was written before this LLM
    # call; nothing else exists yet. ─────────────────────────────────────────
    s0 = client.snapshots[0]["files"]
    assert "version_a.md" in s0
    assert "critic.md" not in s0
    assert "version_b.md" not in s0
    assert "version_ab.md" not in s0
    assert "synth_meta.json" not in s0
    # No judges files yet either.
    assert not any(f.startswith("judges/") for f in s0)

    # ── Snapshot 1 (ARCHITECT_B entry): critic.md is now on disk; version_b
    # is not yet written. ────────────────────────────────────────────────────
    s1 = client.snapshots[1]["files"]
    assert "version_a.md" in s1
    assert "critic.md" in s1
    assert "version_b.md" not in s1
    assert "version_ab.md" not in s1
    assert "synth_meta.json" not in s1

    # ── Snapshot 2 (SYNTHESIZER entry): version_b.md is on disk; synth
    # outputs are not yet written. ───────────────────────────────────────────
    s2 = client.snapshots[2]["files"]
    assert "version_a.md" in s2
    assert "critic.md" in s2
    assert "version_b.md" in s2
    assert "version_ab.md" not in s2
    assert "synth_meta.json" not in s2

    # ── Snapshots 3..5 (the 3 judges, parallel): set-level assertions only,
    # since asyncio.gather may interleave their entries. Every judge sees the
    # full SYNTHESIZER output on disk before its own LLM call fires. ─────────
    for s in client.snapshots[3:6]:
        files = s["files"]
        assert "version_a.md" in files
        assert "critic.md" in files
        assert "version_b.md" in files
        assert "version_ab.md" in files
        assert "synth_meta.json" in files
        # No result.json is written until after ALL judges complete + Borda.
        assert "result.json" not in files

    # ── Post-run state: every per-judge order + response file lands, and
    # result.json is the final artifact written for the pass. ───────────────
    assert (pass1_dir / "version_a.md").exists()
    assert (pass1_dir / "critic.md").exists()
    assert (pass1_dir / "version_b.md").exists()
    assert (pass1_dir / "version_ab.md").exists()
    assert (pass1_dir / "synth_meta.json").exists()
    for i in range(3):
        assert (pass1_dir / "judges" / f"{i}_order.json").exists()
        assert (pass1_dir / "judges" / f"{i}_response.json").exists()
    assert (pass1_dir / "result.json").exists()


# ── Fix 1: hash short-circuit advances streak ────────────────────────────


@pytest.mark.asyncio
async def test_no_change_advances_streak(tmp_path: Path) -> None:
    """When AB wins by Borda but content is byte-stable, the streak advances.

    Reproduces the ``plan-47a530bd`` failure shape: judges always pick AB,
    but the synthesizer returns the incumbent verbatim. Without the
    hash-equality short-circuit (Fix 1) the streak would never advance and
    the loop would burn ``max_rounds``.
    """
    cfg = TournamentConfig(num_judges=3, convergence_k=2, max_rounds=10)
    prefix_map = _prefix_map()

    def _cb(role: str, system: str, user: str) -> str:
        if role == "synthesizer":
            # Return the X version verbatim (which is the incumbent on a
            # coin-flip miss; close enough — both X/Y contain incumbent or
            # B; we want a deterministic byte-stable AB).
            # Simplest: hard-code the same string the test starts with.
            return "MARK_AB_ONLY_STABLE"
        if role != "judge":
            return _role_defaults(role, user)
        return _judge_prefer(user, prefix_map["AB"])

    client = StubLLMClient(fn=_cb)
    artifact = tmp_path / "tournaments" / "no-change"

    t = Tournament(
        handler=_handler(),
        client=client,
        cfg=cfg,
        artifact_dir=artifact,
        rng=random.Random(7),
    )
    _final, history = await t.run("Task.", "MARK_A_ONLY_INITIAL")

    # Pass 1: incumbent flips from initial → MARK_AB_ONLY_STABLE; AB wins
    #   by Borda AND by hash (different content). effective_winner=AB,
    #   streak=0.
    # Pass 2: incumbent already MARK_AB_ONLY_STABLE; synth returns same;
    #   AB wins by Borda but hashes equal → effective_winner=A. streak=1.
    # Pass 3: same → streak=2 → converge.
    assert len(history) == 3
    assert history[0].winner == "AB"
    assert history[0].meta.get("effective_winner") == "AB"
    assert history[1].winner == "AB"
    assert history[1].meta.get("effective_winner") == "A"
    assert history[2].winner == "AB"
    assert history[2].meta.get("effective_winner") == "A"


# ── Fix 6: score-stability runaway detector ──────────────────────────────


@pytest.mark.asyncio
async def test_score_stability_triggers_runaway(tmp_path: Path) -> None:
    """When scores barely change over ``window`` passes, terminate early.

    Constructs a divergent scenario (judges always pick AB, content drifts
    each pass so Fix 1 doesn't short-circuit) where the Borda *scores*
    never deviate by more than ``max_delta`` across the window. The runaway
    detector must fire at the first eligible pass.
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
        if role == "synthesizer":
            state["synth_n"] += 1
            return f"CURRENT_AB_{state['synth_n']}"
        if role != "judge":
            return _role_defaults(role, user)
        return _judge_prefer(user, f"CURRENT_AB_{state['synth_n']}")

    client = StubLLMClient(fn=_cb)
    artifact = tmp_path / "tournaments" / "runaway"
    t = Tournament(
        handler=_handler(),
        client=client,
        cfg=cfg,
        artifact_dir=artifact,
        rng=random.Random(11),
    )
    _final, history = await t.run("Task.", "MARK_A_ONLY_INITIAL")

    # All judges always vote AB-first → AB wins every pass.
    assert all(h.winner == "AB" for h in history)
    # AB's score is the same every pass (each judge gives AB 3 points → 9).
    assert all(h.scores["AB"] == 9 for h in history)
    # Detector fires at pass `window` (=3), so we have exactly 3 passes.
    assert len(history) == 3
    assert history[-1].meta.get("runaway_detected") is True
    # Earlier passes should NOT be marked runaway.
    assert history[0].meta.get("runaway_detected") is None
    assert history[1].meta.get("runaway_detected") is None


@pytest.mark.asyncio
async def test_score_stability_disabled_when_none(tmp_path: Path) -> None:
    """With both knobs ``None`` (default), the detector never fires."""
    cfg = TournamentConfig(
        num_judges=3,
        convergence_k=2,
        max_rounds=4,
        score_stability_window=None,
        score_stability_max_delta=None,
    )
    state: dict[str, int] = {"synth_n": 0}

    def _cb(role: str, system: str, user: str) -> str:
        if role == "synthesizer":
            state["synth_n"] += 1
            return f"CURRENT_AB_{state['synth_n']}"
        if role != "judge":
            return _role_defaults(role, user)
        return _judge_prefer(user, f"CURRENT_AB_{state['synth_n']}")

    client = StubLLMClient(fn=_cb)
    artifact = tmp_path / "tournaments" / "no-detector"
    t = Tournament(
        handler=_handler(),
        client=client,
        cfg=cfg,
        artifact_dir=artifact,
        rng=random.Random(13),
    )
    _final, history = await t.run("Task.", "MARK_A_ONLY_INITIAL")

    # Hits max_rounds, no early termination.
    assert len(history) == cfg.max_rounds
    assert all(h.meta.get("runaway_detected") is None for h in history)


@pytest.mark.asyncio
async def test_score_stability_does_not_fire_when_changing(tmp_path: Path) -> None:
    """When score deltas exceed ``max_delta``, the detector stays quiet."""
    cfg = TournamentConfig(
        num_judges=3,
        convergence_k=10,  # never converge in the small max_rounds we set
        max_rounds=4,
        score_stability_window=3,
        score_stability_max_delta=1,
    )
    # Cycle the favored label each pass so scores swing widely.
    favored_seq = ["A", "B", "AB", "A"]
    state: dict[str, int] = {"synth_n": 0, "pass": 0}
    prefix_map = _prefix_map()

    def _cb(role: str, system: str, user: str) -> str:
        if role == "synthesizer":
            state["synth_n"] += 1
            return f"CURRENT_AB_{state['synth_n']}"
        if role != "judge":
            return _role_defaults(role, user)
        # 3 judges per pass → divide call counter by 3 to derive pass index.
        idx = state.get("judge_n", 0)
        state["judge_n"] = idx + 1
        pass_idx = idx // 3
        target = favored_seq[pass_idx % len(favored_seq)]
        return _judge_prefer(user, prefix_map[target])

    client = StubLLMClient(fn=_cb)
    artifact = tmp_path / "tournaments" / "swinging"
    t = Tournament(
        handler=_handler(),
        client=client,
        cfg=cfg,
        artifact_dir=artifact,
        rng=random.Random(17),
    )
    _final, history = await t.run("Task.", "MARK_A_ONLY_INITIAL")

    # Should run all max_rounds — no runaway termination.
    assert len(history) == cfg.max_rounds
    assert all(h.meta.get("runaway_detected") is None for h in history)


@pytest.mark.asyncio
async def test_change_does_not_short_circuit(tmp_path: Path) -> None:
    """When the synthesizer's output differs each pass, no short-circuit fires.

    The judge callback prefers the slot containing the *current* pass's
    unique synthesizer marker (``CURRENT_AB_<n>``). This isolates the
    judge's preference from the incumbent (which on pass N≥2 contains the
    previous pass's marker, not the current one).
    """
    cfg = TournamentConfig(num_judges=3, convergence_k=2, max_rounds=4)

    state: dict[str, int] = {"synth_n": 0}

    def _cb(role: str, system: str, user: str) -> str:
        if role == "synthesizer":
            state["synth_n"] += 1
            return f"CURRENT_AB_{state['synth_n']}"
        if role != "judge":
            return _role_defaults(role, user)
        # Prefer the slot whose body contains the LATEST synthesizer output.
        return _judge_prefer(user, f"CURRENT_AB_{state['synth_n']}")

    client = StubLLMClient(fn=_cb)
    artifact = tmp_path / "tournaments" / "drifting"
    t = Tournament(
        handler=_handler(),
        client=client,
        cfg=cfg,
        artifact_dir=artifact,
        rng=random.Random(8),
    )
    _final, history = await t.run("Task.", "MARK_A_ONLY_INITIAL")

    # Every pass: hashes differ → effective_winner=AB → streak=0 → hit cap.
    assert len(history) == cfg.max_rounds
    assert all(h.winner == "AB" for h in history)
    assert all(h.meta.get("effective_winner") == "AB" for h in history)
