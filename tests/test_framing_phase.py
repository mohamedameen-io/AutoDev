"""run_framing_phase tests — classifier, generation, panel, resume, gates.

Filled incrementally across Phases 2-6.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator.framing_phase import (
    _extract_classification,
    run_framing_phase,
)
from state.evidence import read_evidence, write_evidence
from state.ledger import read_entries
from state.schemas import FramingEvidence, SolutionApproach
from stub_adapter import StubAdapter, ok


def _bootstrap_repo(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=str(tmp_path), check=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(tmp_path), check=True)
    (tmp_path / "main.py").write_text("def main():\n    return 1\n")
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(tmp_path), check=True)


def _make_orch(cwd: Path, adapter: StubAdapter) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    registry = build_registry(cfg)
    return Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-framing",
    )


def _framing_text(
    classification: str = "local_defect",
    confidence: float = 0.0,
    signals: str = "none",
    hypothesis: str = "stub hypothesis",
) -> str:
    return (
        "```framing\n"
        f"CLASSIFICATION: {classification}\n"
        f"CONFIDENCE: {confidence}\n"
        f"HYPOTHESIS_CHALLENGED: {hypothesis}\n"
        f"SIGNALS_FIRED: {signals}\n"
        "```\n"
    )


# --- parser -----------------------------------------------------------------


def test_parse_framing_response_empty() -> None:
    cls, conf, diags = _extract_classification("")
    assert cls == "local_defect"
    assert conf == 0.0
    assert any("empty response" in d for d in diags)


def test_parse_framing_response_missing_classification() -> None:
    cls, conf, diags = _extract_classification("some prose with no verdict line")
    assert cls == "local_defect"
    assert conf == 0.0


# --- conservatism gate ------------------------------------------------------


@pytest.mark.asyncio
async def test_conservatism_gate_low_confidence(tmp_path: Path) -> None:
    _bootstrap_repo(tmp_path)
    adapter = StubAdapter(
        {"framing": ok(_framing_text("realized_design_failure", 0.6, "recurrence_at_seam"))}
    )
    orch = _make_orch(tmp_path, adapter)
    decision = await run_framing_phase(
        orch, "trim the tool observation", "", "", None, "abc123"
    )
    assert decision is not None
    assert decision.classification == "local_defect"
    assert len(decision.approaches) == 1
    assert decision.approaches[0].altitude == "local_patch"
    assert adapter.count("altitude_judge") == 0


@pytest.mark.asyncio
async def test_conservatism_gate_no_structural_signal(tmp_path: Path) -> None:
    _bootstrap_repo(tmp_path)
    adapter = StubAdapter(
        {"framing": ok(_framing_text("realized_design_failure", 0.9, "none"))}
    )
    orch = _make_orch(tmp_path, adapter)
    decision = await run_framing_phase(orch, "x", "", "", None, "abc123")
    assert decision is not None
    assert decision.classification == "local_defect"
    assert adapter.count("altitude_judge") == 0


# --- evidence + ledger ------------------------------------------------------


@pytest.mark.asyncio
async def test_framing_writes_evidence(tmp_path: Path) -> None:
    _bootstrap_repo(tmp_path)
    adapter = StubAdapter({"framing": ok(_framing_text("local_defect", 0.2))})
    orch = _make_orch(tmp_path, adapter)
    await run_framing_phase(orch, "x", "", "", None, "abc123")
    ev = await read_evidence(tmp_path, "plan-framing", "framing")
    assert isinstance(ev, FramingEvidence)
    assert ev.classification == "local_defect"
    assert (tmp_path / ".autodev" / "evidence" / "plan-framing-framing.json").exists()


@pytest.mark.asyncio
async def test_framing_ledger_classified_op(tmp_path: Path) -> None:
    _bootstrap_repo(tmp_path)
    adapter = StubAdapter({"framing": ok(_framing_text("local_defect", 0.1))})
    orch = _make_orch(tmp_path, adapter)
    await run_framing_phase(orch, "x", "", "", None, "abc123")
    ops = [e.op for e in read_entries(tmp_path)]
    assert "framing_classified" in ops


# --- dispatch ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_framing_dispatch_uses_specialist_path_not_registry(tmp_path: Path) -> None:
    _bootstrap_repo(tmp_path)
    adapter = StubAdapter({"framing": ok(_framing_text("local_defect", 0.1))})
    orch = _make_orch(tmp_path, adapter)
    # framing is deliberately NOT in the registry (build_registry only iterates
    # REQUIRED_AGENT_ROLES); dispatch must bypass it via the specialist path.
    assert "framing" not in orch.registry
    await run_framing_phase(orch, "x", "", "", None, "abc123")
    assert adapter.count("framing") == 1
    assert any(c.role == "framing" for c in adapter.calls)


# --- kill switches ----------------------------------------------------------


@pytest.mark.asyncio
async def test_framing_disabled_via_config(tmp_path: Path) -> None:
    _bootstrap_repo(tmp_path)
    adapter = StubAdapter({})
    orch = _make_orch(tmp_path, adapter)
    orch.cfg.framing.enabled = False
    decision = await run_framing_phase(orch, "x", "", "", None, "abc123")
    assert decision is None
    assert adapter.count("framing") == 0


@pytest.mark.asyncio
async def test_framing_disabled_via_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bootstrap_repo(tmp_path)
    monkeypatch.setenv("AUTODEV_FRAMING_DISABLED", "1")
    adapter = StubAdapter({})
    orch = _make_orch(tmp_path, adapter)
    decision = await run_framing_phase(orch, "x", "", "", None, "abc123")
    assert decision is None
    assert adapter.count("framing") == 0


# --- resume / determinism ---------------------------------------------------


@pytest.mark.asyncio
async def test_framing_resume_skips_classifier(tmp_path: Path) -> None:
    _bootstrap_repo(tmp_path)
    sa = SolutionApproach(
        name="local_patch",
        altitude="local_patch",
        summary="s",
        eliminates_failure_class=False,
        primary_tradeoff="t",
        primary_risk="r",
        est_blast_radius="single function",
    )
    ev = FramingEvidence(
        task_id="plan-framing",
        classification="local_defect",
        confidence=0.0,
        hypothesis_challenged="h",
        approaches=[sa],
        chosen_approach_name="local_patch",
    )
    await write_evidence(tmp_path, "plan-framing", ev)
    adapter = StubAdapter({})  # empty: any framing call would be a fallback
    orch = _make_orch(tmp_path, adapter)
    decision = await run_framing_phase(orch, "x", "", "", None, "abc123")
    assert adapter.count("framing") == 0
    assert decision is not None
    assert decision.classification == "local_defect"
    assert decision.chosen_approach.name == "local_patch"


@pytest.mark.asyncio
async def test_framing_byte_identical_stub(tmp_path: Path) -> None:
    text = _framing_text("local_defect", 0.0)
    d1 = tmp_path / "r1"
    d2 = tmp_path / "r2"
    d1.mkdir()
    d2.mkdir()
    _bootstrap_repo(d1)
    _bootstrap_repo(d2)
    o1 = _make_orch(d1, StubAdapter({"framing": ok(text)}))
    await run_framing_phase(o1, "x", "", "", None, "abc123")
    o2 = _make_orch(d2, StubAdapter({"framing": ok(text)}))
    await run_framing_phase(o2, "x", "", "", None, "abc123")
    raw1 = json.loads(
        (d1 / ".autodev" / "evidence" / "plan-framing-framing.json").read_text()
    )
    raw2 = json.loads(
        (d2 / ".autodev" / "evidence" / "plan-framing-framing.json").read_text()
    )
    assert raw1 == raw2
