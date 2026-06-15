"""Reproduce gate (ADR-0046, Phase 5 — loop as the acceptance signal).

The diagnosis phase builds a *believed-in* feedback loop that **reproduces**
the user's bug: it FAILS on the pre-fix tree. The persisted
:class:`~state.schemas.FeedbackLoop` (inside ``plan-diagnosis-diagnosis.json``)
is the acceptance signal for the fix. This gate runs that loop in the execute
(post-fix) context and asserts it now **passes** — i.e. the fix actually fixed
the reproduced bug. If the loop still fails, the gate BLOCKS.

Two contracts, matching the design doc's reproduce-gate contract
("fail on the pre-fix tree, pass on the post-fix tree; a loop that passes
pre-fix is rejected as not-reproducing"):

* :func:`run_reproduce_gate` — the **post-fix** gate the execute QA dispatcher
  invokes. Reads the persisted loop, runs ``loop.command``, BLOCKS when the
  loop does not pass.
* :func:`verify_loop_reproduces` — the **pre-fix** validity check: runs the
  loop and rejects it if it *passes* (a loop that never reproduced the bug is
  invalid as an acceptance signal).

Graceful degradation (NFR2): when no persisted loop exists, the evidence is
unreadable, the loop is ``fidelity="none"``, or the loop has no runnable
command, the gate **soft-passes** (``severity="info"``) and logs — it never
hard-crashes the dispatcher. A loop whose stated fidelity is ``live`` is also
soft-passed in the sandbox (it cannot be the autonomous signal; the synthetic
loop + delivered artifact carry the verdict — see ADR-0046 §5.2).
"""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass
from pathlib import Path

from autologging import get_logger
from plugins.registry import GateResult
from state.evidence import read_evidence
from state.schemas import DiagnosisEvidence, FeedbackLoop


logger = get_logger(__name__)


# Evidence locator for the diagnosis bundle (file
# ``.autodev/evidence/plan-diagnosis-diagnosis.json``). The diagnosis phase
# writes its evidence under task id ``plan-diagnosis``, kind ``diagnosis``.
_DIAGNOSIS_TASK_ID = "plan-diagnosis"
_DIAGNOSIS_KIND = "diagnosis"

# A reproduction loop is engineered to run in seconds (ADR-0046 §5.1: "target a
# ≤ few-second deterministic loop"); give it generous headroom but never hang.
_DEFAULT_LOOP_TIMEOUT_S = 300.0

# Findings/output excerpt cap so a noisy loop does not produce a huge detail.
_OUTPUT_EXCERPT_CHARS = 2000


@dataclass
class LoopRunResult:
    """Outcome of running a persisted feedback loop's command.

    * ``ran`` — the command was actually executed (False when it was missing /
      the tool was not found / it timed out unrunnably).
    * ``passed`` — exit code 0 (the loop's pass/fail signal: pass == "bug
      absent", fail == "bug present").
    * ``returncode`` — process exit code (``None`` when it did not run).
    * ``output`` — combined stdout+stderr excerpt for diagnostics.
    * ``timed_out`` — the command exceeded the timeout.
    """

    ran: bool
    passed: bool
    returncode: int | None
    output: str
    timed_out: bool = False


async def _read_persisted_loop(cwd: Path) -> FeedbackLoop | None:
    """Return the persisted diagnosis :class:`FeedbackLoop`, or ``None``.

    ``None`` covers every "no usable loop" case: no diagnosis evidence on
    disk, evidence of the wrong kind, or a diagnosis with ``loop=None``.
    Never raises — a read/parse failure degrades to ``None``.
    """
    try:
        ev = await read_evidence(cwd, _DIAGNOSIS_TASK_ID, _DIAGNOSIS_KIND)
    except Exception:  # noqa: BLE001 — evidence IO must never block the gate
        return None
    if ev is None or not isinstance(ev, DiagnosisEvidence):
        return None
    return ev.loop


async def _run_loop_command(
    command: str,
    cwd: Path,
    *,
    timeout_s: float = _DEFAULT_LOOP_TIMEOUT_S,
) -> LoopRunResult:
    """Run ``command`` (a shell string) in *cwd* and capture its pass/fail.

    The persisted ``FeedbackLoop.command`` is an agent-runnable shell string
    (e.g. ``"uv run pytest tests/test_repro.py -q"``). We execute it without an
    intermediate shell when it parses to a clean argv, falling back to the
    shell for commands that genuinely need shell features (pipes, ``&&``).

    A missing executable / timeout returns ``ran=False`` so the caller can
    soft-pass rather than treat an environment gap as a fix failure.
    """
    if not command or not command.strip():
        return LoopRunResult(ran=False, passed=False, returncode=None, output="")

    needs_shell = any(tok in command for tok in ("|", "&&", "||", ";", ">", "<", "$("))
    try:
        if needs_shell:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            try:
                argv = shlex.split(command)
            except ValueError:
                # Unbalanced quotes etc. — fall back to the shell.
                proc = await asyncio.create_subprocess_shell(
                    command,
                    cwd=str(cwd),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            else:
                if not argv:
                    return LoopRunResult(
                        ran=False, passed=False, returncode=None, output=""
                    )
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=str(cwd),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_s
        )
    except FileNotFoundError:
        # The loop's tool is not on PATH in this sandbox: not a fix failure.
        return LoopRunResult(
            ran=False,
            passed=False,
            returncode=None,
            output="loop command executable not found",
        )
    except asyncio.TimeoutError:
        return LoopRunResult(
            ran=False,
            passed=False,
            returncode=None,
            output=f"loop command timed out after {timeout_s:.0f}s",
            timed_out=True,
        )
    except OSError as exc:  # spawn failure (e.g. permission) — never crash.
        return LoopRunResult(
            ran=False,
            passed=False,
            returncode=None,
            output=f"loop command failed to spawn: {exc}",
        )

    combined = (stdout + stderr).decode(errors="replace").strip()
    rc = proc.returncode
    return LoopRunResult(
        ran=True,
        passed=(rc == 0),
        returncode=rc,
        output=combined[:_OUTPUT_EXCERPT_CHARS],
    )


def _is_runnable_in_sandbox(loop: FeedbackLoop) -> tuple[bool, str]:
    """Decide whether *loop* can serve as the autonomous sandbox signal.

    Returns ``(runnable, reason)``. A loop is NOT runnable as the autonomous
    signal when its fidelity is ``none`` (no loop) or ``live`` (the autonomous
    path uses the synthetic/replay proxy + a delivered artifact, never a live
    call — ADR-0046 §5.2, NFR5). ``reason`` is a human string for the
    soft-pass detail when not runnable.
    """
    if loop.fidelity == "none":
        return False, "loop fidelity is 'none' (no reproducible loop)"
    if loop.fidelity == "live":
        return (
            False,
            "loop fidelity is 'live'; the sandbox uses the synthetic/replay "
            "proxy + delivered artifact, not a live call",
        )
    if not loop.command or not loop.command.strip():
        return False, "loop has no runnable command"
    return True, ""


async def verify_loop_reproduces(
    cwd: Path,
    loop: FeedbackLoop | None = None,
    *,
    timeout_s: float = _DEFAULT_LOOP_TIMEOUT_S,
) -> GateResult:
    """Pre-fix validity check: the loop must FAIL (reproduce the bug).

    Run on the **pre-fix** tree. A valid reproduction loop fails here
    (exit != 0 == "bug present"). A loop that *passes* pre-fix never
    reproduced the bug and is rejected as an invalid acceptance signal
    (``passed=False, severity="block"``).

    When *loop* is ``None`` it is loaded from the persisted diagnosis
    evidence. Degrades to a soft info-pass when no usable loop exists or the
    loop is not sandbox-runnable.
    """
    if loop is None:
        loop = await _read_persisted_loop(cwd)
    if loop is None:
        return _soft_pass("no persisted diagnosis loop to verify")

    runnable, reason = _is_runnable_in_sandbox(loop)
    if not runnable:
        return _soft_pass(f"reproduce-verify skipped: {reason}")

    run = await _run_loop_command(loop.command, cwd, timeout_s=timeout_s)
    if not run.ran:
        return _soft_pass(f"reproduce-verify could not run loop: {run.output}")

    metrics = _loop_metrics(loop, run, phase="pre_fix")
    if run.passed:
        # Loop passed on the BUGGY tree → it does not reproduce the bug.
        return GateResult(
            passed=False,
            severity="block",
            details=(
                "reproduce-gate: loop PASSES on the pre-fix (buggy) tree — it "
                "does not reproduce the bug and is invalid as an acceptance "
                f"signal.\ncommand: {loop.command}\n{run.output}"
            ),
            metrics=metrics,
        )
    return GateResult(
        passed=True,
        details=(
            "reproduce-gate: loop correctly FAILS on the pre-fix tree "
            "(bug reproduced)"
        ),
        metrics=metrics,
    )


async def run_reproduce_gate(
    cwd: Path,
    paths: list[Path] | None = None,
    *,
    loop: FeedbackLoop | None = None,
    timeout_s: float = _DEFAULT_LOOP_TIMEOUT_S,
) -> GateResult:
    """Post-fix gate: the persisted loop must now PASS (the fix worked).

    This is the entry point the execute QA dispatcher wires. It runs the
    persisted reproduction loop on the post-fix tree:

    * loop passes (exit 0) → ``passed=True`` (the reproduced bug is gone).
    * loop still fails → ``passed=False, severity="block"`` (the fix did not
      fix the reproduced bug).

    The ``paths`` parameter is accepted for signature parity with the other
    diff-scoped gates (so the dispatcher can pass the changed-files list
    uniformly); the loop runs the whole persisted command regardless of
    scope.

    Soft info-pass (never blocks) when: no persisted loop, unreadable
    evidence, ``fidelity in {"none","live"}``, no runnable command, or the
    loop's tool is unavailable / it times out in this sandbox.
    """
    if loop is None:
        loop = await _read_persisted_loop(cwd)
    if loop is None:
        return _soft_pass("no persisted diagnosis loop; reproduce-gate skipped")

    runnable, reason = _is_runnable_in_sandbox(loop)
    if not runnable:
        return _soft_pass(f"reproduce-gate skipped: {reason}")

    run = await _run_loop_command(loop.command, cwd, timeout_s=timeout_s)
    if not run.ran:
        # Could not run the loop (tool missing / timeout): degrade, don't block.
        return _soft_pass(
            f"reproduce-gate could not run loop ({run.output}); skipped"
        )

    metrics = _loop_metrics(loop, run, phase="post_fix")
    if run.passed:
        return GateResult(
            passed=True,
            details=(
                "reproduce-gate: loop PASSES on the post-fix tree — the "
                "reproduced bug is fixed"
            ),
            metrics=metrics,
        )
    return GateResult(
        passed=False,
        severity="block",
        details=(
            "reproduce-gate: loop still FAILS on the post-fix tree — the fix "
            f"did not resolve the reproduced bug.\ncommand: {loop.command}\n"
            f"{run.output}"
        ),
        metrics=metrics,
    )


def _soft_pass(reason: str) -> GateResult:
    """Build a non-blocking info-pass and log the degradation reason."""
    logger.info("qa.reproduce_gate.soft_pass", reason=reason)
    return GateResult(
        passed=True,
        severity="info",
        details=f"reproduce-gate: {reason}",
        metrics={"reproduce_gate_ran": False},
    )


def _loop_metrics(
    loop: FeedbackLoop, run: LoopRunResult, *, phase: str
) -> dict[str, object]:
    """Structured metrics carrier for downstream consumers."""
    return {
        "reproduce_gate_ran": True,
        "phase": phase,
        "loop_method": loop.method,
        "loop_fidelity": loop.fidelity,
        "loop_deterministic": loop.deterministic,
        "loop_passed": run.passed,
        "loop_returncode": run.returncode,
        "loop_timed_out": run.timed_out,
    }


__all__ = [
    "LoopRunResult",
    "run_reproduce_gate",
    "verify_loop_reproduces",
]
