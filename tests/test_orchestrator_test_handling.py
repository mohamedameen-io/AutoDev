"""Tests for the v0.32.0 Phase 3 diagnosis-aware test handling in
:func:`orchestrator.execute_phase`.

Pins three contracts:

  * ``no_tests_found`` does not soft-block — task proceeds to ``tested``.
  * Repeated infrastructure-class failures (``collection_failed``)
    retry once then hard-fail with a structured ``blocked_reason``.
  * ``no_signal`` soft-blocks with the explicit reason
    "test result inconclusive — no diagnostic signal" rather than
    masquerading as a generic test failure.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from adapters.types import AgentResult
from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from state.schemas import (
    AcceptanceCriterion,
    Phase,
    Plan,
    Task,
)

from stub_adapter import StubAdapter, ok


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan() -> Plan:
    tasks = [
        Task(
            id="1.1",
            phase_id="1",
            title="Add subtract",
            description="Implement subtract(a, b)",
            files=["math.py"],
            acceptance=[
                AcceptanceCriterion(id="ac-1", description="tests pass"),
            ],
        ),
    ]
    return Plan(
        plan_id="p-test-handling",
        spec_hash="d",
        phases=[Phase(id="1", title="Work", tasks=tasks)],
        created_at=_iso(),
        updated_at=_iso(),
    )


async def _make_orch_with_plan(
    cwd: Path, adapter: StubAdapter
) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    registry = build_registry(cfg)
    orch = Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-test-handling",
    )
    await orch.plan_manager.init_plan(_mk_plan())
    return orch


def _coder_ok_with_diff(variant: str = "v1") -> AgentResult:
    """Stub developer result. ``variant`` perturbs the diff so the loop
    detector (which hashes adapter output) doesn't trip across retries
    on the infrastructure-class diagnosis tests."""
    return AgentResult(
        success=True,
        text=f"wrote subtract ({variant})",
        diff=(
            "diff --git a/math.py b/math.py\n"
            "--- a/math.py\n"
            "+++ b/math.py\n"
            "@@ -0,0 +1 @@\n"
            f"+def subtract(a,b): return a-b  # {variant}\n"
        ),
        files_changed=[Path("math.py")],
        duration_s=0.1,
    )


def _reviewer_approved() -> AgentResult:
    return ok("APPROVED\n- clean")


@pytest.mark.asyncio
async def test_no_tests_found_does_not_soft_block(tmp_path: Path) -> None:
    """``no_tests_found`` is a legitimate state — proceed, don't block.

    Test runner returns success=True with "no tests" in output and
    zero counts. The classifier should diagnose ``no_tests_found``,
    the orchestrator should mark the task ``tested`` (not blocked),
    and the task should reach ``complete`` with the metadata flag
    ``test_gate=no_tests_found``.
    """
    test_runner_output = ok("INFO: no tests found in scope\nVERDICT: SKIPPED")

    adapter = StubAdapter(
        {
            "developer": _coder_ok_with_diff(),
            "reviewer": _reviewer_approved(),
            "test_engineer": test_runner_output,
        }
    )
    orch = await _make_orch_with_plan(tmp_path, adapter)
    tasks = await orch.execute()

    assert len(tasks) == 1
    final = tasks[0]
    # The task proceeded all the way to complete — NOT blocked.
    assert final.status == "complete", (
        f"expected complete, got {final.status} "
        f"(blocked_reason={final.blocked_reason})"
    )
    # And the test gate did NOT retry — only one test_engineer call.
    assert adapter.count("test_engineer") == 1
    # No spurious blocked_reason on the happy path.
    assert final.blocked_reason is None


@pytest.mark.asyncio
async def test_collection_failed_retries_then_hard_fails(
    tmp_path: Path,
) -> None:
    """``collection_failed`` retries once then hard-fails with diagnosis.

    Two consecutive ``collection_failed`` results should produce a
    blocked task whose ``blocked_reason`` carries the diagnosis and
    whose metadata carries the structured ``test_diagnosis`` key.
    """
    collection_fail = AgentResult(
        success=False,
        text="ERROR collecting tests/foo.py: ImportError: missing dep",
        raw_stderr="collection failure during conftest evaluation",
        error="pytest collection failed",
        duration_s=0.01,
    )

    adapter = StubAdapter(
        {
            "developer": [_coder_ok_with_diff("v1"), _coder_ok_with_diff("v2")],
            "reviewer": _reviewer_approved(),
            "test_engineer": [collection_fail, collection_fail],
        }
    )
    orch = await _make_orch_with_plan(tmp_path, adapter)
    tasks = await orch.execute()

    assert len(tasks) == 1
    final = tasks[0]
    assert final.status == "blocked"
    blocked_reason = final.blocked_reason or ""
    assert "test_diagnosis" in blocked_reason
    assert "collection_failed" in blocked_reason
    # Exactly two test_engineer invocations: one initial + one retry.
    assert adapter.count("test_engineer") == 2


@pytest.mark.asyncio
async def test_no_signal_soft_blocks_with_explicit_reason(
    tmp_path: Path,
) -> None:
    """``no_signal`` soft-blocks with an explicit "inconclusive" reason.

    Catch-all for genuinely uninformative failures — should NOT
    masquerade as ``capture_failed`` or ``runtime_crash``; the user-
    facing reason must say "test result inconclusive".
    """
    no_signal = AgentResult(
        success=False,
        text="some uninformative output without recognised cues",
        raw_stderr="",
        error=None,
        duration_s=0.01,
    )

    adapter = StubAdapter(
        {
            "developer": _coder_ok_with_diff(),
            "reviewer": _reviewer_approved(),
            "test_engineer": no_signal,
        }
    )
    orch = await _make_orch_with_plan(tmp_path, adapter)
    tasks = await orch.execute()

    assert len(tasks) == 1
    final = tasks[0]
    assert final.status == "blocked"
    blocked_reason = final.blocked_reason or ""
    assert "test result inconclusive" in blocked_reason
    # Single invocation — no_signal does not retry.
    assert adapter.count("test_engineer") == 1


@pytest.mark.asyncio
async def test_no_tests_found_persists_diagnosis_in_evidence(
    tmp_path: Path,
) -> None:
    """The TestEvidence file carries ``diagnosis=no_tests_found``.

    Downstream consumers (e.g. ``autodev status --blocked`` in
    Phase 5) read the evidence JSON to surface diagnoses to operators
    — pin the on-disk contract.
    """
    import json

    test_runner_output = ok("INFO: no tests collected")

    adapter = StubAdapter(
        {
            "developer": _coder_ok_with_diff(),
            "reviewer": _reviewer_approved(),
            "test_engineer": test_runner_output,
        }
    )
    orch = await _make_orch_with_plan(tmp_path, adapter)
    await orch.execute()

    evfile = tmp_path / ".autodev" / "evidence" / "1.1-test.json"
    assert evfile.exists()
    data = json.loads(evfile.read_text())
    assert data.get("diagnosis") == "no_tests_found"
