"""No-degrade integration: real dispatch through intake (+ diagnosis) (Cluster C1).

This is the test that would have caught the Run-4 DEAD-ON-ARRIVAL. The previous
DOA was masked because the unit tests monkeypatched ``run_intake_phase`` /
``_invoke_intake_role`` (so the real ``cfg.agents[role]`` KeyError never fired)
and the fail-safe degrade swallowed the KeyError at runtime — every field run
silently produced ``degraded=True`` with ``spec == raw intent``.

Here we drive the REAL :func:`run_intake_phase` and :func:`run_diagnosis_phase`
through the REAL specialist dispatch (``_invoke_intake_role`` /
``_invoke_diagnostician`` reading ``orch.cfg.agents[role]``) against a
``StubAdapter`` that stubs ONLY the LLM roles (no network, no subprocess). We
assert the outcomes are NOT degraded and that real enrichment evidence was
produced (``spec != raw intent``). If the loader backfill regresses (the
specialist-role ``cfg.agents`` entry goes missing), the dispatch KeyErrors, the
fail-safe degrades, and these assertions fail — exactly the signal Run-4 lacked.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator.diagnosis_phase import run_diagnosis_phase
from orchestrator.intake_phase import run_intake_phase
from state.evidence import write_evidence
from state.schemas import ExploreEvidence
from stub_adapter import StubAdapter, ok


# An under-specified bug intent: triggers BOTH the intake gap path (missing
# acceptance/constraints/touchpoints) AND the diagnosis is-bug-fix gate ("bug").
RAW_INTENT = "the thing is broken, fix the bug"

ENRICHED_SPEC = """\
## Scope: fix the regression in bar()
The reported crash is a bug in src/foo.py:bar() which drops the trailing token.

## Acceptance
- bar("a,b,") must return ["a", "b", ""] (expected behavior)

## Constraints
- must preserve the existing public signature (backwards compatible)

## Touchpoints
- src/foo.py:bar()
"""

DIAGNOSTICIAN_RESPONSE = """\
SYMPTOM: bar() drops the trailing empty token
REPRODUCED: true
LOOP_METHOD: failing_test
LOOP_COMMAND: pytest tests/test_foo.py::test_bar -q
LOOP_FIDELITY: synthetic
LOOP_DETERMINISTIC: true
HYPOTHESIS 1: split() with default maxsplit drops trailing empties || a test on "a,b," fails
CONFIRMED_CAUSE: bar() uses str.split which discards the trailing empty field
SEAM: correct
RECURRENCE_AT_SEAM: false
"""


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
        session_id="sess-no-degrade",
    )


def _adapter() -> StubAdapter:
    """Stub ONLY the LLM roles intake/diagnosis dispatch. No clarifying questions
    (clarifier returns an empty/no ``questions`` block) so the headless policy is
    a no-op and the spec lock is the enriched draft."""
    return StubAdapter(
        {
            # gather dispatch (intake_enricher is reused as the gather role too):
            # first call gathers facts, the enrich call returns the enriched spec.
            # A single handler returning the enriched spec also yields no parsable
            # ```facts block for the gather call, which degrades gather to [] —
            # fine: enrichment still runs and the spec still differs from intent.
            "intake_enricher": ok(ENRICHED_SPEC),
            "intake_clarifier": ok("no constraint-level questions worth asking"),
            "diagnostician": ok(DIAGNOSTICIAN_RESPONSE),
        }
    )


@pytest.mark.asyncio
async def test_intake_not_degraded_real_dispatch(tmp_path: Path) -> None:
    """run_intake_phase through the REAL specialist dispatch is NOT degraded and
    produces real enrichment (spec != raw intent)."""
    _bootstrap_repo(tmp_path)
    adapter = _adapter()
    orch = _make_orch(tmp_path, adapter)

    outcome = await run_intake_phase(orch, RAW_INTENT)

    # The DOA signature was degraded=True (KeyError-at-dispatch → fail-safe).
    assert outcome.degraded is False
    # Real enrichment evidence: the locked spec is the enriched draft, NOT the
    # raw intent (the gap path actually ran the enricher).
    assert outcome.spec.strip() != RAW_INTENT.strip()
    assert "## Scope" in outcome.spec
    # The enricher role was actually dispatched (real dispatch, no monkeypatch).
    assert adapter.count("intake_enricher") >= 1
    assert adapter.count("intake_clarifier") >= 1
    # The spec was locked to disk.
    assert (tmp_path / ".autodev" / "spec.md").exists()


@pytest.mark.asyncio
async def test_intake_passthrough_is_not_degraded(tmp_path: Path) -> None:
    """A well-formed spec passes through (no LLM) and is NOT marked degraded —
    degrade is reserved for the disabled / kill-switch / error paths."""
    _bootstrap_repo(tmp_path)
    adapter = _adapter()
    orch = _make_orch(tmp_path, adapter)

    well_formed = (
        "## Scope: fix bar() in src/foo.py\n"
        "bar() must return the trailing empty token (expected behavior).\n"
        "Constraint: keep the public signature backwards compatible.\n"
    )
    outcome = await run_intake_phase(orch, well_formed)
    assert outcome.degraded is False
    assert outcome.passthrough is True
    # Pass-through spends zero LLM calls.
    assert adapter.count("intake_enricher") == 0


@pytest.mark.asyncio
async def test_diagnosis_ran_real_dispatch(tmp_path: Path) -> None:
    """run_diagnosis_phase through the REAL ``diagnostician`` dispatch actually
    RAN (outcome.ran is True, not the disabled/not-bug degrade) and confirmed a
    cause — the diagnosis half of the Run-4 DOA."""
    _bootstrap_repo(tmp_path)
    adapter = _adapter()
    orch = _make_orch(tmp_path, adapter)

    outcome = await run_diagnosis_phase(
        orch, ENRICHED_SPEC, explore_ev="bar() drops the trailing token"
    )

    # DiagnosisOutcome.ran is False only for disabled / not_bug_fix degrades.
    assert outcome.ran is True
    assert outcome.reason == "ok"
    assert outcome.confirmed_cause is not None
    assert outcome.seam == "correct"
    # Real dispatch happened (no monkeypatch of _invoke_diagnostician).
    assert adapter.count("diagnostician") == 1
    assert (
        tmp_path / ".autodev" / "evidence" / "plan-diagnosis-diagnosis.json"
    ).exists()


@pytest.mark.asyncio
async def test_intake_then_diagnosis_chain_no_degrade(tmp_path: Path) -> None:
    """End-to-end through BOTH phases on the bug path: intake enriches (not
    degraded), then diagnosis runs against the enriched spec (ran=True). This is
    the full Run-4 path that silently no-op'd."""
    _bootstrap_repo(tmp_path)
    # Explorer evidence lets the repo gather source activate too (reuse path).
    await write_evidence(
        tmp_path,
        "plan-explore",
        ExploreEvidence(
            task_id="plan-explore",
            findings="bar() in src/foo.py uses str.split and drops trailing tokens",
            files_referenced=["src/foo.py"],
        ),
    )
    adapter = _adapter()
    orch = _make_orch(tmp_path, adapter)

    intake_outcome = await run_intake_phase(orch, RAW_INTENT)
    assert intake_outcome.degraded is False
    assert intake_outcome.spec.strip() != RAW_INTENT.strip()

    diag_outcome = await run_diagnosis_phase(
        orch, intake_outcome.spec, explore_ev="bar() drops the trailing token"
    )
    assert diag_outcome.ran is True
    assert diag_outcome.confirmed_cause is not None
