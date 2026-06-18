"""Non-vacuity gate test for reproduce_gate evidence-READ failures.

Closes WS3-reproduce-gate-soft-pass-on-ioerror.

The defect: ``run_reproduce_gate`` reads the persisted diagnosis evidence via
``_read_persisted_loop`` -> ``read_evidence``. ``read_evidence`` swallows BOTH
a *missing* file and a *present-but-unreadable* (corrupt JSON / IOError) file to
``None``. The gate then treats ``None`` as "no usable loop" and SOFT-PASSES.

That is the bug: a corrupt evidence file (the acceptance signal we cannot read)
must NOT be silently waved through as a pass. We must distinguish:

* MISSING (legitimately absent)            -> soft info-pass (existing behavior)
* UNREADABLE-BUT-EXISTS (corrupt / IOError) -> passed=False, BLOCK (fail-loud)

Note on severity: the task brief asks for ``severity="error"``, but
``plugins.registry.GateResult.severity`` is ``Literal["info","warn","block"]``
(no ``"error"`` member, and that file is not ours to edit). The blocking value
that the dispatcher routes as "halt the task" is ``severity="block"`` with
``passed=False`` — that is the fail-loud signal we assert here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from qa.reproduce_gate import run_reproduce_gate, verify_loop_reproduces
from state.paths import evidence_path


_DIAGNOSIS_TASK_ID = "plan-diagnosis"
_DIAGNOSIS_KIND = "diagnosis"


def _diagnosis_evidence_path(cwd: Path) -> Path:
    return evidence_path(cwd, _DIAGNOSIS_TASK_ID, _DIAGNOSIS_KIND)


def _write_corrupt_evidence(cwd: Path) -> Path:
    """Create a present-but-unreadable diagnosis evidence file.

    Invalid JSON: ``read_evidence`` raises ``json.JSONDecodeError`` internally
    and degrades to ``None`` — the exact code path that produced the spurious
    soft-pass before the fix.
    """
    p = _diagnosis_evidence_path(cwd)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ this is NOT valid json !!!  ", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# RED-on-HEAD: unreadable-but-exists must FAIL LOUD, not soft-pass.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unreadable_evidence_blocks_not_soft_pass(tmp_path: Path) -> None:
    """A corrupt diagnosis evidence file that EXISTS → block, not soft-pass.

    RED on HEAD: today the gate returns passed=True / severity=info (a
    spurious soft-pass) because read_evidence swallows the parse error to None.
    """
    _write_corrupt_evidence(tmp_path)

    result = await run_reproduce_gate(tmp_path)

    assert result.passed is False, (
        "corrupt-but-present evidence must NOT soft-pass — a gate that passes "
        "because it could not read its acceptance signal is the bug"
    )
    assert result.severity == "block", (
        "unreadable evidence is a fail-loud condition, not an info note"
    )
    # The verdict must announce the unreadable-evidence cause, not a generic skip.
    assert "unreadable" in result.details.lower() or "corrupt" in result.details.lower()
    # Honesty metric: the gate did not actually run a loop.
    assert result.metrics.get("reproduce_gate_ran") is False


@pytest.mark.asyncio
async def test_unreadable_evidence_blocks_on_prefix_verify(tmp_path: Path) -> None:
    """The pre-fix verifier must also fail loud on unreadable evidence."""
    _write_corrupt_evidence(tmp_path)

    result = await verify_loop_reproduces(tmp_path)

    assert result.passed is False
    assert result.severity == "block"
    assert "unreadable" in result.details.lower() or "corrupt" in result.details.lower()


# ---------------------------------------------------------------------------
# CONTROL (non-vacuity): genuinely MISSING evidence keeps the soft-pass.
#
# This is what makes the test above non-vacuous: it proves the fix
# DISTINGUISHES missing from unreadable rather than blanket-failing every
# read-returns-None case. If the fix collapsed both branches into "block",
# this control would turn RED (the broken-control proof).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_evidence_still_soft_passes(tmp_path: Path) -> None:
    """No evidence file at all → existing soft/skip behavior is preserved."""
    # tmp_path has no .autodev/evidence/plan-diagnosis-diagnosis.json
    assert not _diagnosis_evidence_path(tmp_path).exists()

    result = await run_reproduce_gate(tmp_path)

    assert result.passed is True
    assert result.severity == "info"
    assert result.metrics.get("reproduce_gate_ran") is False


@pytest.mark.asyncio
async def test_missing_evidence_soft_passes_on_prefix_verify(tmp_path: Path) -> None:
    """Pre-fix verifier soft-passes on a genuinely missing file too."""
    assert not _diagnosis_evidence_path(tmp_path).exists()

    result = await verify_loop_reproduces(tmp_path)

    assert result.passed is True
    assert result.severity == "info"


# ---------------------------------------------------------------------------
# IOError on a present file (not just bad JSON) → also block.
# Monkeypatch open to raise on the present diagnosis path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ioerror_on_present_evidence_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A present evidence file whose READ raises IOError → block, not soft-pass.

    Distinct from the bad-JSON case: here the file is valid-but-unreadable
    (e.g. permissions / a transient IOError). The gate must still fail loud.
    """
    # Make the file exist (content irrelevant — the read will be forced to raise).
    target = _diagnosis_evidence_path(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}", encoding="utf-8")

    real_read_text = Path.read_text

    def _boom_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self == target:
            raise OSError("simulated unreadable evidence (EIO)")
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", _boom_read_text)

    result = await run_reproduce_gate(tmp_path)

    assert result.passed is False
    assert result.severity == "block"
    assert result.metrics.get("reproduce_gate_ran") is False


# ---------------------------------------------------------------------------
# Sanity: a VALID present loop still runs (the fix did not break the happy path).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_present_evidence_still_runs(tmp_path: Path) -> None:
    """A readable, valid persisted loop still executes (no over-blocking)."""
    from state.evidence import write_evidence
    from state.schemas import DiagnosisEvidence, FeedbackLoop

    (tmp_path / "FIXED").write_text("done\n")
    py = sys.executable
    body = "import os,sys; sys.exit(0 if os.path.exists('FIXED') else 1)"
    loop = FeedbackLoop(
        method="failing_test",  # type: ignore[arg-type]
        command=f'{py} -c "{body}"',
        fidelity="synthetic",  # type: ignore[arg-type]
        deterministic=True,
    )
    ev = DiagnosisEvidence(
        task_id=_DIAGNOSIS_TASK_ID,
        loop=loop,
        reproduced=True,
        symptom="boom",
        loop_fidelity="synthetic",  # type: ignore[arg-type]
    )
    await write_evidence(tmp_path, _DIAGNOSIS_TASK_ID, ev)

    result = await run_reproduce_gate(tmp_path)

    assert result.passed is True
    assert result.metrics.get("reproduce_gate_ran") is True
