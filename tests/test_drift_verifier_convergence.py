"""v0.34.0 B3: drift-verifier convergence-failure exit condition.

Covers four cases from the v0.34 plan:

* Two near-identical corrective diffs trip the convergence guard.
* A genuinely different corrective patch does NOT trip it.
* Convergence failures route to the existing phase-review escalation
  path (i.e. produce `passed=False` with `convergence_failure=True`).
* `_patch_similarity` returns 0.0 on empty inputs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from adapters.types import AgentSpec
from orchestrator.drift_verifier import (
    DriftVerdict,
    _patch_similarity,
    run_drift_verifier,
)
from state.schemas import AcceptanceCriterion, Phase, Task

from stub_adapter import StubAdapter, ok


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
        id="2",
        title="x",
        description="y",
        tasks=[Task(id="2.1", phase_id="2", title="t", description="d")],
        acceptance=[AcceptanceCriterion(id="2.a", description="acc")],
    )


_DIFF_A = """diff --git a/foo.py b/foo.py
index 1111..2222 100644
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,5 @@
 def foo():
+    print("hello")
+    return 42
     pass
"""

_DIFF_A_PRIME = """diff --git a/foo.py b/foo.py
index 1111..3333 100644
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,5 @@
 def foo():
+    print("hello")
+    return 42
     pass
"""

_DIFF_B = """diff --git a/bar.py b/bar.py
index aaaa..bbbb 100644
--- a/bar.py
+++ b/bar.py
@@ -1,2 +1,5 @@
 def bar():
+    x = 1
+    y = 2
+    return x + y
"""


def test_patch_similarity_returns_zero_for_empty_inputs() -> None:
    assert _patch_similarity("", "") == 0.0
    assert _patch_similarity("", _DIFF_A) == 0.0
    assert _patch_similarity(_DIFF_A, "") == 0.0


def test_patch_similarity_returns_high_for_near_identical() -> None:
    sim = _patch_similarity(_DIFF_A, _DIFF_A_PRIME)
    assert sim >= 0.90


def test_patch_similarity_returns_low_for_distinct_patches() -> None:
    sim = _patch_similarity(_DIFF_A, _DIFF_B)
    assert sim < 0.5


@pytest.mark.asyncio
async def test_drift_verifier_detects_convergence_failure_on_identical_patches(
    tmp_path: Path,
) -> None:
    """≥90% similarity between prior and current corrective trips the guard."""
    adapter = StubAdapter({"critic_drift_verifier": ok("VERDICT: APPROVED\n")})
    orch = _OrchStub(adapter=adapter, cwd=tmp_path)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    verdict = await run_drift_verifier(
        orch=orch,
        phase=_phase(),
        evidence_dir=evidence_dir,
        diff_text=_DIFF_A_PRIME,
        prior_corrective_diff=_DIFF_A,
        attempt=2,
    )
    assert isinstance(verdict, DriftVerdict)
    assert verdict.passed is False
    assert verdict.convergence_failure is True
    assert any("drift_convergence_failure" in f for f in verdict.drift_findings)


@pytest.mark.asyncio
async def test_drift_verifier_does_not_trip_on_low_similarity_corrective(
    tmp_path: Path,
) -> None:
    """A genuinely different corrective patch falls through to the agent."""
    adapter = StubAdapter({"critic_drift_verifier": ok("VERDICT: APPROVED\n")})
    orch = _OrchStub(adapter=adapter, cwd=tmp_path)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    verdict = await run_drift_verifier(
        orch=orch,
        phase=_phase(),
        evidence_dir=evidence_dir,
        diff_text=_DIFF_B,
        prior_corrective_diff=_DIFF_A,
        attempt=1,
    )
    assert verdict.convergence_failure is False


@pytest.mark.asyncio
async def test_drift_convergence_failure_routes_to_escalation(
    tmp_path: Path,
) -> None:
    """A convergence-failure verdict carries the fields the phase-review
    runner consumes to flip ``accept_phase`` to False and synthesize a
    corrective_direction."""
    adapter = StubAdapter({"critic_drift_verifier": ok("VERDICT: APPROVED\n")})
    orch = _OrchStub(adapter=adapter, cwd=tmp_path)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    verdict = await run_drift_verifier(
        orch=orch,
        phase=_phase(),
        evidence_dir=evidence_dir,
        diff_text=_DIFF_A_PRIME,
        prior_corrective_diff=_DIFF_A,
        attempt=2,
    )
    assert verdict.passed is False
    assert verdict.drift_findings  # non-empty so the runner can render bullets
    # The verdict.evidence_path must exist so the orchestrator's
    # ledger-append breadcrumb can record a relative path.
    assert verdict.evidence_path.exists()
