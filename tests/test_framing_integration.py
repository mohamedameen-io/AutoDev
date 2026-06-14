"""Integration tests: framing wired into run_plan_phase (Phase 5).

These exercise the flag-guarded call site, the architect-context threading, the
digest-object passing, fail-safe degradation, evidence-before-architect ordering, and
deterministic-on-resume (zero framing calls). Framing degrades to ``local_defect`` via
the StubAdapter fallback unless a test stubs it otherwise.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from state.evidence import write_evidence
from state.schemas import FramingEvidence, SolutionApproach

from stub_adapter import StubAdapter, ok


CANONICAL_PLAN_MD = """# Plan: Add subtract(a, b)

## Phase 1: Implement

### Task 1.1: Add subtract function to math.py
  - Description: Add subtract(a, b) that returns a - b
  - Files: math.py
  - Acceptance:
    - [ ] Function subtract defined
"""


def _make_orch(
    cwd: Path, adapter: StubAdapter, *, index_enabled: bool = False
) -> Orchestrator:
    cfg = default_config()
    cfg.index_enabled = index_enabled
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    return Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=build_registry(cfg),
        session_id="sess-framing-integration",
    )


def _base_adapter() -> StubAdapter:
    return StubAdapter(
        {
            "explorer": ok("found stuff"),
            "domain_expert": ok("ok"),
            "architect": ok(CANONICAL_PLAN_MD),
        }
    )


@pytest.mark.asyncio
async def test_plan_phase_with_framing_disabled_unchanged(tmp_path: Path) -> None:
    """cfg.framing.enabled=False → no framing call; plan output unchanged."""
    adapter = _base_adapter()
    orch = _make_orch(tmp_path, adapter)
    orch.cfg.framing.enabled = False
    plan = await orch.plan("Add subtract(a, b)")
    assert plan is not None
    assert len(plan.phases) == 1
    assert adapter.count("framing") == 0


@pytest.mark.asyncio
async def test_plan_phase_threads_chosen_strategy_into_architect(
    tmp_path: Path,
) -> None:
    adapter = _base_adapter()
    orch = _make_orch(tmp_path, adapter)  # framing on by default
    await orch.plan("Add subtract(a, b)")
    architect_prompt = adapter.prompts_for("architect")[0]
    # DelegationEnvelope renders context as ``  key: value`` lines.
    assert "chosen_strategy:" in architect_prompt
    assert "framing_classification:" in architect_prompt


@pytest.mark.asyncio
async def test_plan_phase_passes_digest_object_to_framing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guards the string-vs-object regression: framing must receive the
    structured CandidateDigest, not the rendered ``candidate_digest_str``."""
    from state.file_index import CandidateDigest, FileHit
    from orchestrator import plan_phase as pp

    autodev = tmp_path / ".autodev"
    autodev.mkdir(parents=True, exist_ok=True)
    (autodev / "index.db").write_bytes(b"fake-sqlite-bytes")

    captured: dict[str, object] = {}

    async def _capture(**kwargs: object) -> None:
        captured["digest"] = kwargs["candidate_digest"]
        return None

    monkeypatch.setattr(pp, "run_framing_phase", _capture)

    digest = CandidateDigest(file_hits=[FileHit(path="math.py", lang="py")])
    fake_query = mock.MagicMock()
    fake_query.get_candidates_for_spec.return_value = digest
    fake_index_module = mock.MagicMock()
    fake_index_module.IndexQuery = mock.MagicMock(return_value=fake_query)

    adapter = _base_adapter()
    with mock.patch.dict("sys.modules", {"state.file_index": fake_index_module}):
        orch = _make_orch(tmp_path, adapter, index_enabled=True)
        await orch.plan("Add subtract(a, b)")

    assert isinstance(captured["digest"], CandidateDigest)
    assert not isinstance(captured["digest"], str)


@pytest.mark.asyncio
async def test_plan_phase_framing_failure_is_fail_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orchestrator import plan_phase as pp

    async def _boom(**kwargs: object) -> None:
        raise RuntimeError("framing exploded")

    monkeypatch.setattr(pp, "run_framing_phase", _boom)
    adapter = _base_adapter()
    orch = _make_orch(tmp_path, adapter)
    plan = await orch.plan("Add subtract(a, b)")
    assert plan is not None  # framing failure must NOT block planning
    architect_prompt = adapter.prompts_for("architect")[0]
    assert "chosen_strategy: local_patch" in architect_prompt


@pytest.mark.asyncio
async def test_framing_evidence_written_before_architect(tmp_path: Path) -> None:
    adapter = _base_adapter()  # framing uses the StubAdapter fallback
    orch = _make_orch(tmp_path, adapter)
    await orch.plan("Add subtract(a, b)")
    roles = [c.role for c in adapter.calls]
    assert "framing" in roles and "architect" in roles
    assert roles.index("framing") < roles.index("architect")
    assert (
        tmp_path / ".autodev" / "evidence" / "plan-framing-framing.json"
    ).exists()


@pytest.mark.asyncio
async def test_resume_through_plan_phase_zero_framing_calls(tmp_path: Path) -> None:
    """Pre-written framing evidence → resume re-read skips the classifier
    (zero framing adapter calls); the architect context reflects the cache."""
    sa_local = SolutionApproach(
        name="trim",
        altitude="local_patch",
        summary="s",
        eliminates_failure_class=False,
        primary_tradeoff="t",
        primary_risk="r",
        est_blast_radius="single function",
    )
    sa_design = SolutionApproach(
        name="redesign",
        altitude="design_fix",
        summary="s",
        eliminates_failure_class=True,
        primary_tradeoff="t",
        primary_risk="r",
        est_blast_radius="cross-module contract",
    )
    ev = FramingEvidence(
        task_id="plan-framing",
        classification="realized_design_failure",
        confidence=0.9,
        hypothesis_challenged="h",
        signals_fired=["recurrence_at_seam"],
        approaches=[sa_local, sa_design],
        chosen_approach_name="redesign",
        altitude_rationale="eliminates the class",
    )
    await write_evidence(tmp_path, "plan-framing", ev)
    adapter = _base_adapter()  # framing NOT stubbed — a call would prove non-resume
    orch = _make_orch(tmp_path, adapter)
    await orch.plan("Add subtract(a, b)")
    assert adapter.count("framing") == 0
    architect_prompt = adapter.prompts_for("architect")[0]
    assert "framing_classification: realized_design_failure" in architect_prompt
    assert "chosen_strategy: redesign" in architect_prompt
