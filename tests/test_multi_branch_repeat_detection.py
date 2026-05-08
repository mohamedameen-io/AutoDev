"""v0.17.0 S4: multi-branch repeated-hypothesis tagging integration.

Validates that :func:`run_multi_branch_plan_tournament` invokes the
:class:`RepeatedHypothesisDetector` before the asyncio.gather and tags
matching branches with ``BranchOutcome.metadata["hypothesis_repeat"] = True``.
The check is advisory: branches still execute regardless of the tag.

These tests focus on the detection-and-tagging path; the underlying
:func:`run_plan_tournament` is mocked because the integration surface
is "did the dispatcher build BranchOutcomes with the right metadata,
and did the ledger op fire."
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config.defaults import default_config
from config.schema import BranchConfig
from state.knowledge import KnowledgeStore, TournamentEvent


@pytest.mark.asyncio
async def test_repeat_detected_tags_branch_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orchestrator import multi_branch_tournament as mbt

    # Build minimal fakes for orch / plan_manager / knowledge.
    cfg = default_config()
    knowledge = KnowledgeStore(tmp_path, cfg=cfg)
    # Record a prior discard whose hypothesis text equals the family
    # string. The dispatcher uses ``branch_config.family`` as the
    # hypothesis when set; bigram-Jaccard self-similarity is 1.0.
    await knowledge.record_tournament_event(
        TournamentEvent(
            event_type="discard",
            family="distant-scout",
            hypothesis="distant-scout",
            evidence="prior failure",
        )
    )

    class _FakePM:
        async def ledger_append(self, op: str, payload: dict) -> None:
            self.calls.append((op, payload))

        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

    pm = _FakePM()

    class _FakeOrch:
        pass

    orch = _FakeOrch()
    orch.cwd = tmp_path  # type: ignore[attr-defined]
    orch.cfg = default_config()  # type: ignore[attr-defined]
    orch.plan_manager = pm  # type: ignore[attr-defined]
    orch.knowledge = knowledge  # type: ignore[attr-defined]

    # Stub out the per-branch tournament runner so we don't actually
    # spawn a tournament — just return canned final markdown.
    async def fake_run(*args, **kwargs):  # noqa: ARG001
        return "FINAL\n"

    monkeypatch.setattr(mbt, "_run_one_branch", fake_run)
    # Also stub the meta-merge step.
    async def fake_meta(*args, **kwargs):  # noqa: ARG001
        return "META\n", []

    monkeypatch.setattr(mbt, "_meta_merge_pairwise", fake_meta)
    # Stub _build_meta_role_overrides + _resolve_meta_model — meta-merge
    # is short-circuited but the function still constructs metadata.
    monkeypatch.setattr(
        mbt, "_build_meta_role_overrides", lambda *a, **k: ({}, {}, {}, {})
    )
    monkeypatch.setattr(mbt, "_resolve_meta_model", lambda *a, **k: None)

    branch_configs = [
        BranchConfig(family="distant-scout", lane="distant-scout"),
        BranchConfig(family="local-tweak", lane="local-tweak"),
    ]
    # Initial markdown is irrelevant when family is set: detector uses family.
    initial_md = "irrelevant"

    outcome = await mbt.run_multi_branch_plan_tournament(
        orch,  # type: ignore[arg-type]
        initial_md,
        spec="test",
        spec_hash="0123456789abcdef" * 4,
        n_branches=2,
        branch_configs=branch_configs,
    )

    # Branch 0's family ("distant-scout") matches the recorded discard.
    assert outcome.branches[0].metadata.get("hypothesis_repeat") is True
    # Branch 1's family ("local-tweak") does not match.
    assert outcome.branches[1].metadata.get("hypothesis_repeat", False) is False

    # Ledger op was emitted for branch 0.
    repeat_ops = [c for c in pm.calls if c[0] == "hypothesis_repeat_detected"]
    assert len(repeat_ops) == 1
    assert repeat_ops[0][1]["branch_index"] == 0


@pytest.mark.asyncio
async def test_repeat_detection_disabled_when_threshold_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``repeated_hypothesis_threshold = 0`` disables the check entirely."""
    from orchestrator import multi_branch_tournament as mbt

    cfg_overridden = default_config().model_copy(
        update={"repeated_hypothesis_threshold": 0.0}
    )
    knowledge = KnowledgeStore(tmp_path, cfg=cfg_overridden)
    # Even with a strong prior discard, the check is skipped at threshold=0.
    await knowledge.record_tournament_event(
        TournamentEvent(
            event_type="discard",
            family="distant-scout",
            hypothesis="any text",
            evidence="x",
        )
    )

    class _FakePM:
        async def ledger_append(self, op: str, payload: dict) -> None:
            self.calls.append((op, payload))

        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

    pm = _FakePM()

    class _FakeOrch:
        pass

    orch = _FakeOrch()
    orch.cwd = tmp_path  # type: ignore[attr-defined]
    orch.cfg = cfg_overridden  # type: ignore[attr-defined]
    orch.plan_manager = pm  # type: ignore[attr-defined]
    orch.knowledge = knowledge  # type: ignore[attr-defined]

    async def fake_run(*args, **kwargs):  # noqa: ARG001
        return "FINAL\n"

    monkeypatch.setattr(mbt, "_run_one_branch", fake_run)

    async def fake_meta(*args, **kwargs):  # noqa: ARG001
        return "META\n", []

    monkeypatch.setattr(mbt, "_meta_merge_pairwise", fake_meta)
    monkeypatch.setattr(
        mbt, "_build_meta_role_overrides", lambda *a, **k: ({}, {}, {}, {})
    )
    monkeypatch.setattr(mbt, "_resolve_meta_model", lambda *a, **k: None)

    branch_configs = [
        BranchConfig(family="distant-scout"),
        BranchConfig(family="distant-scout"),
    ]
    outcome = await mbt.run_multi_branch_plan_tournament(
        orch,  # type: ignore[arg-type]
        "x",
        spec="t",
        spec_hash="0" * 64,
        n_branches=2,
        branch_configs=branch_configs,
    )

    # No metadata tags, no ledger ops.
    assert outcome.branches[0].metadata == {}
    assert outcome.branches[1].metadata == {}
    repeat_ops = [c for c in pm.calls if c[0] == "hypothesis_repeat_detected"]
    assert repeat_ops == []
