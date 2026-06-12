"""Tests: non-standard 'PASS' / 'PARTIAL PASS' verdicts treated as NEEDS_REVISION.

Regression coverage for task 0.c2: the critic_drift_verifier agent sometimes
emits 'VERDICT: PASS' or 'VERDICT: PARTIAL PASS' (forbidden by the agent
prompt, but tolerated by the parser). The orchestrator must classify these as
NEEDS_REVISION rather than falling back to the 'missing VERDICT line' path,
and must surface the raw verdict text in the finding message.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from adapters.types import AgentSpec
from orchestrator.drift_verifier import _parse_drift_response, run_drift_verifier
from state.schemas import AcceptanceCriterion, Phase, Task

from stub_adapter import StubAdapter, ok


# ── helpers ────────────────────────────────────────────────────────────────


class _OrchStub:
    def __init__(self, adapter: StubAdapter, cwd: Path) -> None:
        self.adapter = adapter
        self.cwd = cwd
        self.registry = {
            "critic_drift_verifier": AgentSpec(
                name="critic_drift_verifier",
                description="phase drift verifier",
                prompt="(stub prompt)",
                tools=["read", "glob", "grep"],
                model=None,
                max_turns=3,
            )
        }


def _phase() -> Phase:
    return Phase(
        id="0",
        title="Research phase",
        description="explore codebase",
        tasks=[Task(id="0.1", phase_id="0", title="read files", description="grep stuff")],
        acceptance=[AcceptanceCriterion(id="0.a", description="findings documented")],
    )


# ── _parse_drift_response unit tests ───────────────────────────────────────


def test_parse_verdict_pass_is_not_approved() -> None:
    """'VERDICT: PASS' must not set passed=True."""
    passed, findings = _parse_drift_response("VERDICT: PASS\n")
    assert passed is False


def test_parse_verdict_pass_adds_nonstandard_finding() -> None:
    """'VERDICT: PASS' must produce a non-standard-verdict finding, NOT a
    'missing VERDICT line' finding — the parser saw the line."""
    passed, findings = _parse_drift_response("VERDICT: PASS\n")
    assert any("non-standard verdict" in f for f in findings), (
        f"expected non-standard verdict finding, got: {findings}"
    )
    assert not any("missing VERDICT line" in f for f in findings), (
        f"unexpected missing-VERDICT fallback finding in: {findings}"
    )


def test_parse_verdict_pass_finding_contains_raw_text() -> None:
    """The finding message must include the actual raw verdict string 'PASS'
    so the evidence file is diagnostic."""
    _, findings = _parse_drift_response("VERDICT: PASS\n")
    assert any("PASS" in f for f in findings), (
        f"expected 'PASS' in findings, got: {findings}"
    )


def test_parse_verdict_partial_pass_is_not_approved() -> None:
    """'VERDICT: PARTIAL PASS' (as produced by the real evidence file) must
    set passed=False."""
    passed, findings = _parse_drift_response(
        "## PHASE VERDICT\n"
        "**VERDICT: PARTIAL PASS — 11 of 12 criteria fully confirmed.**\n"
    )
    assert passed is False


def test_parse_verdict_partial_pass_nonstandard_finding() -> None:
    """'VERDICT: PARTIAL PASS' must surface a non-standard finding, not
    fall back to the 'missing VERDICT line' path."""
    _, findings = _parse_drift_response(
        "## PHASE VERDICT\n"
        "**VERDICT: PARTIAL PASS — 11 of 12 criteria fully confirmed.**\n"
    )
    assert any("non-standard verdict" in f for f in findings), (
        f"expected non-standard verdict finding, got: {findings}"
    )
    assert not any("missing VERDICT line" in f for f in findings), (
        f"unexpected missing-VERDICT fallback in: {findings}"
    )


def test_parse_verdict_pass_lowercase() -> None:
    """Lowercase 'pass' must also be classified as non-standard."""
    passed, findings = _parse_drift_response("verdict: pass\n")
    assert passed is False
    assert any("non-standard verdict" in f for f in findings), (
        f"expected non-standard verdict finding, got: {findings}"
    )


def test_parse_verdict_fail_treated_as_nonstandard() -> None:
    """'VERDICT: FAIL' must also be non-standard (not APPROVED or NEEDS_REVISION)."""
    passed, findings = _parse_drift_response("VERDICT: FAIL\n")
    assert passed is False
    assert any("non-standard verdict" in f for f in findings), (
        f"expected non-standard verdict finding, got: {findings}"
    )


def test_parse_approved_with_all_tasks_verified_passes() -> None:
    """Positive control: standard APPROVED with no MISSING/DRIFTED tasks passes."""
    passed, findings = _parse_drift_response(
        "TASK 0.1: VERIFIED\n"
        "VERDICT: APPROVED\n"
    )
    assert passed is True
    assert findings == []


def test_parse_verdict_pass_with_missing_task_findings_preserved() -> None:
    """When 'PASS' is accompanied by MISSING task lines, both the non-standard
    finding AND the task findings must be present."""
    passed, findings = _parse_drift_response(
        "TASK 0.1: MISSING\n"
        "VERDICT: PASS\n"
    )
    assert passed is False
    assert any("non-standard verdict" in f for f in findings)
    assert any("0.1" in f and "MISSING" in f for f in findings), (
        f"expected task 0.1 MISSING finding, got: {findings}"
    )


# ── run_drift_verifier integration tests ───────────────────────────────────


@pytest.mark.asyncio
async def test_run_drift_verifier_pass_verdict_fails(tmp_path: Path) -> None:
    """End-to-end: critic returns 'VERDICT: PASS' → DriftVerdict.passed is False."""
    adapter = StubAdapter(
        {
            "critic_drift_verifier": ok(
                "TASK 0.1: VERIFIED\n"
                "## PHASE VERDICT\n"
                "VERDICT: PASS\n"
            )
        }
    )
    orch = _OrchStub(adapter, tmp_path)
    verdict = await run_drift_verifier(
        orch=orch,
        phase=_phase(),
        evidence_dir=tmp_path / "evidence",
        diff_text="",
    )
    assert verdict.passed is False
    assert any("non-standard verdict" in f for f in verdict.drift_findings), (
        f"expected non-standard verdict finding, got: {verdict.drift_findings}"
    )


@pytest.mark.asyncio
async def test_run_drift_verifier_partial_pass_verdict_fails(tmp_path: Path) -> None:
    """End-to-end: critic returns the exact 'PARTIAL PASS' string from the
    evidence file → DriftVerdict.passed is False with non-standard finding."""
    partial_pass_response = (
        "| AC1 | ✅ PASS |\n"
        "| AC4 | ⚠️ PARTIAL |\n\n"
        "**VERDICT: PARTIAL PASS — 11 of 12 criteria fully confirmed. "
        "One gap (AC4): per-line enumeration not produced.**"
    )
    adapter = StubAdapter({"critic_drift_verifier": ok(partial_pass_response)})
    orch = _OrchStub(adapter, tmp_path)
    verdict = await run_drift_verifier(
        orch=orch,
        phase=_phase(),
        evidence_dir=tmp_path / "evidence",
        diff_text="",
    )
    assert verdict.passed is False
    assert not any("missing VERDICT line" in f for f in verdict.drift_findings), (
        f"unexpected missing-VERDICT fallback: {verdict.drift_findings}"
    )
    assert any("non-standard verdict" in f for f in verdict.drift_findings), (
        f"expected non-standard verdict finding: {verdict.drift_findings}"
    )


@pytest.mark.asyncio
async def test_run_drift_verifier_nonstandard_verdict_evidence_written(
    tmp_path: Path,
) -> None:
    """Evidence JSON must record passed=False when verdict is non-standard."""
    import json

    adapter = StubAdapter(
        {"critic_drift_verifier": ok("VERDICT: PASS\n")}
    )
    orch = _OrchStub(adapter, tmp_path)
    evidence_dir = tmp_path / "evidence"
    verdict = await run_drift_verifier(
        orch=orch,
        phase=_phase(),
        evidence_dir=evidence_dir,
        diff_text="",
    )
    assert verdict.evidence_path.exists()
    payload = json.loads(verdict.evidence_path.read_text(encoding="utf-8"))
    assert payload["passed"] is False
    assert any("non-standard verdict" in f for f in payload["drift_findings"])
