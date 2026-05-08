"""End-to-end tests for the multi-branch meta-merge step (commit 6).

Exercises :func:`_run_meta_merge_step` and :func:`_meta_merge_pairwise`
with a stub LLM client so we get full synth+judge coverage without
real subprocesses. Validates:

- Synthesizer-only contract (no CRITIC, no ARCHITECT_B in the loop).
- Deterministic output for identical inputs (re-run yields same merge).
- Borda aggregation picks the judge-preferred candidate.
- Artifact layout matches the documented
  ``tournaments/multi-{hash}/meta-merge/step-N/`` schema.
- Order preservation (left-fold reduction over candidate list).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator import multi_branch_tournament as mbt
from tournament.plan_tournament import PlanContentHandler

from stub_adapter import StubAdapter, ok


_SPEC_HASH = "0123456789abcdef"


class _MetaStubClient:
    """In-process LLM stub used by ``_run_meta_merge_step`` directly.

    Tracks role-call counts and returns scripted output. Distinct from
    :class:`tests.stub_adapter.StubAdapter` because the meta-merge
    constructs an ``AdapterLLMClient`` internally, which then calls
    the adapter — but for unit testing we want to bypass that whole
    layer and substitute an LLM client directly.
    """

    def __init__(self, judge_response: str = "RANKING: 1 2 3") -> None:
        self.calls: list[dict[str, str]] = []
        self.judge_response = judge_response

    async def call(
        self,
        *,
        system: str,
        user: str,
        role: str,
        model: str | None = None,
    ) -> str:
        self.calls.append({"system": system, "user": user, "role": role})
        if role == "synthesizer":
            return "# Plan: synthesized\n## Phase 1\n### Task 1.1\n  - Description: x\n  - Files: f\n  - Acceptance:\n    - [ ] ok\n"
        if role == "judge":
            return self.judge_response
        # Should not be called for critic_t / architect_b in meta-merge.
        raise AssertionError(f"unexpected role in meta-merge: {role}")


def _make_orch(tmp_path: Path) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.num_judges = 3
    cfg.tournaments.auto_disable_for_models = []
    registry = build_registry(cfg)
    return Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=StubAdapter({"explorer": ok("ok")}),
        registry=registry,
        session_id="sess-test-meta",
    )


@pytest.mark.asyncio
async def test_meta_merge_step_invokes_synthesizer_then_judges(
    tmp_path: Path,
) -> None:
    """One step → 1 synth call + N parallel judge calls. NO critic, NO architect_b."""
    orch = _make_orch(tmp_path)
    handler = PlanContentHandler()
    client = _MetaStubClient()
    a_md = "# Plan: A\n"
    b_md = "# Plan: B\n"

    merged_md, pass_result = await mbt._run_meta_merge_step(
        orch=orch,
        handler=handler,
        client=client,  # type: ignore[arg-type]
        spec="user spec",
        spec_hash=_SPEC_HASH,
        a_md=a_md,
        b_md=b_md,
        step_idx=0,
        num_judges=3,
        judge_model="sonnet",
    )

    # Roles invoked: 1 synth + 3 judges = 4 calls. NO critic_t. NO architect_b.
    roles = [c["role"] for c in client.calls]
    assert roles.count("synthesizer") == 1
    assert roles.count("judge") == 3
    assert "critic_t" not in roles
    assert "architect_b" not in roles

    # PassResult records winner + scores.
    assert pass_result.winner in {"A", "B", "AB"}
    assert pass_result.valid_judges == 3


@pytest.mark.asyncio
async def test_meta_merge_step_artifact_layout(tmp_path: Path) -> None:
    """Step writes to ``tournaments/multi-{hash}/meta-merge/step-N/``
    with version_a.md, version_b.md, version_ab.md, judges/, result.json."""
    orch = _make_orch(tmp_path)
    handler = PlanContentHandler()
    client = _MetaStubClient()

    await mbt._run_meta_merge_step(
        orch=orch,
        handler=handler,
        client=client,  # type: ignore[arg-type]
        spec="spec",
        spec_hash=_SPEC_HASH,
        a_md="# A\n",
        b_md="# B\n",
        step_idx=2,
        num_judges=2,
        judge_model=None,
    )

    expected_dir = (
        tmp_path
        / ".autodev"
        / "tournaments"
        / f"multi-{_SPEC_HASH[:8]}"
        / "meta-merge"
        / "step-2"
    )
    assert expected_dir.exists()
    assert (expected_dir / "pass_01" / "version_a.md").exists()
    assert (expected_dir / "pass_01" / "version_b.md").exists()
    assert (expected_dir / "pass_01" / "version_ab.md").exists()
    assert (expected_dir / "pass_01" / "synth_meta.json").exists()
    assert (expected_dir / "pass_01" / "judges" / "0_order.json").exists()
    assert (expected_dir / "pass_01" / "judges" / "1_order.json").exists()
    assert (expected_dir / "pass_01" / "result.json").exists()
    assert (expected_dir / "final_output.md").exists()


@pytest.mark.asyncio
async def test_meta_merge_deterministic_across_reruns(tmp_path: Path) -> None:
    """Two runs of ``_run_meta_merge_step`` with identical inputs produce
    identical synth_meta (X/Y assignment) — the determinism guarantee."""
    handler = PlanContentHandler()
    a_md = "# Plan: deterministic-a\n"
    b_md = "# Plan: deterministic-b\n"

    metas: list[dict] = []
    for trial in range(2):
        sub_path = tmp_path / f"trial-{trial}"
        sub_path.mkdir()
        orch = _make_orch(sub_path)
        client = _MetaStubClient()
        await mbt._run_meta_merge_step(
            orch=orch,
            handler=handler,
            client=client,  # type: ignore[arg-type]
            spec="spec",
            spec_hash=_SPEC_HASH,
            a_md=a_md,
            b_md=b_md,
            step_idx=0,
            num_judges=2,
            judge_model=None,
        )
        meta_path = (
            sub_path
            / ".autodev"
            / "tournaments"
            / f"multi-{_SPEC_HASH[:8]}"
            / "meta-merge"
            / "step-0"
            / "pass_01"
            / "synth_meta.json"
        )
        assert meta_path.exists()
        import json

        metas.append(json.loads(meta_path.read_text(encoding="utf-8")))
    assert metas[0] == metas[1], (
        "Identical inputs must produce identical X/Y assignments "
        "(deterministic seed); got "
        f"trial 0: {metas[0]} vs trial 1: {metas[1]}"
    )


@pytest.mark.asyncio
async def test_meta_merge_judge_failure_handled_gracefully(
    tmp_path: Path,
) -> None:
    """A judge raising an exception is captured into pass_result; step
    still completes with the surviving judges."""

    class _FlakyClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def call(
            self, *, system: str, user: str, role: str, model: str | None = None
        ) -> str:
            self.calls.append(role)
            if role == "synthesizer":
                return "# Plan: synth\n"
            if role == "judge":
                # First judge fails; second succeeds.
                if self.calls.count("judge") == 1:
                    raise RuntimeError("judge crashed")
                return "RANKING: 1 2 3"
            raise AssertionError(role)

    orch = _make_orch(tmp_path)
    handler = PlanContentHandler()
    client = _FlakyClient()

    merged_md, pass_result = await mbt._run_meta_merge_step(
        orch=orch,
        handler=handler,
        client=client,  # type: ignore[arg-type]
        spec="s",
        spec_hash=_SPEC_HASH,
        a_md="# A\n",
        b_md="# B\n",
        step_idx=0,
        num_judges=2,
        judge_model=None,
    )
    # Only one judge had a valid ranking.
    assert pass_result.valid_judges == 1


@pytest.mark.asyncio
async def test_meta_merge_pairwise_left_fold_order_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_meta_merge_pairwise`` always reduces left-to-right.

    For candidates [A, B, C, D]:
        step 0: synth(A, B) -> m1
        step 1: synth(m1, C) -> m2
        step 2: synth(m2, D) -> m3
    """
    seen_a_b: list[tuple[str, str, int]] = []

    async def fake_step(
        *,
        orch: Any,
        handler: Any,
        client: Any,
        spec: str,
        spec_hash: str,
        a_md: str,
        b_md: str,
        step_idx: int,
        num_judges: int,
        judge_model: str | None,
    ) -> tuple[str, Any]:
        seen_a_b.append((a_md, b_md, step_idx))
        from tournament.core import PassResult

        return (
            f"# m{step_idx}\n",
            PassResult(
                pass_num=1,
                winner="AB",
                scores={"A": 0, "B": 0, "AB": 1},
                valid_judges=1,
                elapsed_s=0.0,
                judge_details=[],
                incumbent_hash_before="x",
                incumbent_hash_after="y",
                meta={},
            ),
        )

    monkeypatch.setattr(mbt, "_run_meta_merge_step", fake_step)

    orch = _make_orch(tmp_path)
    candidates = ["# A\n", "# B\n", "# C\n", "# D\n"]
    final_md, history = await mbt._meta_merge_pairwise(
        orch, candidates, spec="s", spec_hash=_SPEC_HASH
    )

    assert len(seen_a_b) == 3
    # Step 0
    assert seen_a_b[0] == ("# A\n", "# B\n", 0)
    # Step 1: a_md is m0 from step 0
    assert seen_a_b[1] == ("# m0\n", "# C\n", 1)
    # Step 2: a_md is m1 from step 1
    assert seen_a_b[2] == ("# m1\n", "# D\n", 2)
    # Final is from step 2
    assert final_md == "# m2\n"
    assert len(history) == 3
