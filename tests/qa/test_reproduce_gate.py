"""Reproduce gate (ADR-0046, Phase 5 — loop as acceptance signal).

Asserts the reproduce-gate contract:

* Pre-fix: a valid loop FAILS (reproduces the bug) → ``verify_loop_reproduces``
  passes. A loop that PASSES pre-fix is rejected as invalid.
* Post-fix: a loop that now PASSES → ``run_reproduce_gate`` passes (fix worked).
  A loop that still FAILS → BLOCK (fix did not fix it).
* The full red→green cycle: same loop fails pre-fix, passes post-fix.
* Graceful degradation (soft info-pass, never block) when: no persisted loop,
  fidelity in {"none","live"}, no command, or the loop tool is unavailable.
* ``loop_fidelity`` is never reported as ``live`` on the autonomous path.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from qa.reproduce_gate import (
    run_reproduce_gate,
    verify_loop_reproduces,
)
from state.evidence import write_evidence
from state.schemas import DiagnosisEvidence, FeedbackLoop


# A loop whose pass/fail flips on the presence of a ``FIXED`` marker file in
# cwd. Pre-fix: marker absent → exit 1 (bug present, loop FAILS). Post-fix:
# marker present → exit 0 (bug gone, loop PASSES). Uses only the running
# interpreter so it is fully sandbox-runnable with no external tooling.
def _flipping_loop_command() -> str:
    py = sys.executable
    body = "import os,sys; sys.exit(0 if os.path.exists('FIXED') else 1)"
    return f'{py} -c "{body}"'


def _always_passes_command() -> str:
    py = sys.executable
    return f'{py} -c "import sys; sys.exit(0)"'


def _make_loop(
    command: str,
    *,
    fidelity: str = "synthetic",
    method: str = "failing_test",
) -> FeedbackLoop:
    return FeedbackLoop(
        method=method,  # type: ignore[arg-type]
        command=command,
        fidelity=fidelity,  # type: ignore[arg-type]
        deterministic=True,
    )


async def _persist_loop(cwd: Path, loop: FeedbackLoop) -> None:
    ev = DiagnosisEvidence(
        task_id="plan-diagnosis",
        loop=loop,
        reproduced=True,
        symptom="boom",
        loop_fidelity=loop.fidelity,
    )
    await write_evidence(cwd, "plan-diagnosis", ev)


# ---------------------------------------------------------------------------
# Pre-fix verification (verify_loop_reproduces): loop must FAIL.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prefix_loop_that_fails_is_valid(tmp_path: Path) -> None:
    """A loop that fails on the buggy (pre-fix) tree is a valid repro."""
    loop = _make_loop(_flipping_loop_command())
    # No FIXED marker → loop fails → reproduces the bug.
    result = await verify_loop_reproduces(tmp_path, loop)
    assert result.passed is True
    assert result.metrics["loop_passed"] is False
    assert result.metrics["phase"] == "pre_fix"


@pytest.mark.asyncio
async def test_prefix_loop_that_passes_is_rejected(tmp_path: Path) -> None:
    """A loop that PASSES pre-fix never reproduced the bug → rejected."""
    loop = _make_loop(_always_passes_command())
    result = await verify_loop_reproduces(tmp_path, loop)
    assert result.passed is False
    assert result.severity == "block"
    assert "does not reproduce" in result.details


# ---------------------------------------------------------------------------
# Post-fix gate (run_reproduce_gate): loop must PASS.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postfix_loop_that_passes_succeeds(tmp_path: Path) -> None:
    """On the post-fix tree the loop passes → gate passes."""
    (tmp_path / "FIXED").write_text("done\n")  # simulate the applied fix
    loop = _make_loop(_flipping_loop_command())
    result = await run_reproduce_gate(tmp_path, loop=loop)
    assert result.passed is True
    assert result.metrics["loop_passed"] is True
    assert result.metrics["phase"] == "post_fix"


@pytest.mark.asyncio
async def test_postfix_loop_that_still_fails_blocks(tmp_path: Path) -> None:
    """A loop that still fails post-fix means the fix didn't work → BLOCK."""
    # No FIXED marker → loop still fails.
    loop = _make_loop(_flipping_loop_command())
    result = await run_reproduce_gate(tmp_path, loop=loop)
    assert result.passed is False
    assert result.severity == "block"
    assert "did not resolve" in result.details


@pytest.mark.asyncio
async def test_full_red_green_cycle(tmp_path: Path) -> None:
    """Same loop: fails pre-fix (red), passes post-fix (green)."""
    loop = _make_loop(_flipping_loop_command())

    # RED: pre-fix verification passes because the loop correctly fails.
    pre = await verify_loop_reproduces(tmp_path, loop)
    assert pre.passed is True
    assert pre.metrics["loop_passed"] is False

    # Apply the "fix".
    (tmp_path / "FIXED").write_text("done\n")

    # GREEN: post-fix gate passes because the loop now passes.
    post = await run_reproduce_gate(tmp_path, loop=loop)
    assert post.passed is True
    assert post.metrics["loop_passed"] is True


# ---------------------------------------------------------------------------
# Persisted-loop reading.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reads_persisted_loop_from_evidence(tmp_path: Path) -> None:
    """When no loop is passed, it is read from the persisted diagnosis evidence."""
    (tmp_path / "FIXED").write_text("done\n")
    loop = _make_loop(_flipping_loop_command())
    await _persist_loop(tmp_path, loop)

    result = await run_reproduce_gate(tmp_path)  # no explicit loop
    assert result.passed is True
    assert result.metrics["reproduce_gate_ran"] is True


# ---------------------------------------------------------------------------
# Graceful degradation (soft info-pass, never block).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_persisted_loop_soft_passes(tmp_path: Path) -> None:
    """No diagnosis evidence on disk → soft info-pass, never blocks."""
    result = await run_reproduce_gate(tmp_path)
    assert result.passed is True
    assert result.severity == "info"
    assert result.metrics["reproduce_gate_ran"] is False


@pytest.mark.asyncio
async def test_fidelity_none_soft_passes(tmp_path: Path) -> None:
    """A loop with fidelity='none' (no reproducible loop) → soft-pass."""
    loop = _make_loop(_always_passes_command(), fidelity="none")
    result = await run_reproduce_gate(tmp_path, loop=loop)
    assert result.passed is True
    assert result.severity == "info"
    assert result.metrics["reproduce_gate_ran"] is False


@pytest.mark.asyncio
async def test_fidelity_live_soft_passes_not_run(tmp_path: Path) -> None:
    """A 'live' loop is never run autonomously (NFR5) → soft-pass."""
    loop = _make_loop(_always_passes_command(), fidelity="live")
    result = await run_reproduce_gate(tmp_path, loop=loop)
    assert result.passed is True
    assert result.severity == "info"
    # Honesty: we did not run a live loop in the sandbox.
    assert result.metrics["reproduce_gate_ran"] is False
    assert "live" in result.details


@pytest.mark.asyncio
async def test_unavailable_tool_soft_passes(tmp_path: Path) -> None:
    """A loop whose tool is not on PATH degrades to soft-pass, never blocks."""
    loop = _make_loop("this-tool-does-not-exist-xyz --run")
    result = await run_reproduce_gate(tmp_path, loop=loop)
    assert result.passed is True
    assert result.severity == "info"
    assert result.metrics["reproduce_gate_ran"] is False


@pytest.mark.asyncio
async def test_empty_command_soft_passes(tmp_path: Path) -> None:
    """A loop with an empty command is not runnable → soft-pass."""
    loop = _make_loop("   ")
    result = await run_reproduce_gate(tmp_path, loop=loop)
    assert result.passed is True
    assert result.severity == "info"


@pytest.mark.asyncio
async def test_metrics_never_report_live_on_autonomous_run(tmp_path: Path) -> None:
    """When the gate actually runs a loop, its fidelity is never 'live'."""
    (tmp_path / "FIXED").write_text("done\n")
    loop = _make_loop(_flipping_loop_command(), fidelity="synthetic")
    result = await run_reproduce_gate(tmp_path, loop=loop)
    assert result.metrics["reproduce_gate_ran"] is True
    assert result.metrics["loop_fidelity"] != "live"
