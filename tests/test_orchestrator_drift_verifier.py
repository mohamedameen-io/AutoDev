"""Tests for the v0.16.0 drift-verifier wiring.

The wiring layer is :mod:`orchestrator.drift_verifier`. The agent prompt
itself (``src/agents/prompts/critic_drift_verifier.md``) is unchanged in
v0.16.0 — these tests only exercise the runner that builds the
:class:`DelegationEnvelope`, dispatches the critic via the platform
adapter, parses the verdict, and persists evidence.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from adapters.types import AgentInvocation, AgentSpec
from state.schemas import AcceptanceCriterion, Phase, Task

from stub_adapter import StubAdapter, ok


class _OrchStub:
    """Minimal Orchestrator stand-in for the drift-verifier helper.

    Only exposes attributes the helper reads. Concrete production
    orchestrator wiring (PlanManager, KnowledgeStore, etc.) is irrelevant
    for these tests.
    """

    def __init__(self, adapter: StubAdapter, cwd: Path) -> None:
        self.adapter = adapter
        self.cwd = cwd
        # The drift verifier reads the agent spec from this dict; it does
        # NOT need a full registry, just the one entry it dispatches.
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
        title="Implement feature X",
        description="add a function `feature_x` to module foo",
        tasks=[
            Task(
                id="2.1",
                phase_id="2",
                title="add feature_x",
                description="implement feature_x in foo.py",
            ),
        ],
        acceptance=[
            AcceptanceCriterion(
                id="2.a", description="feature_x exists in foo.py and is exported"
            ),
        ],
    )


# ── tests ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_drift_verifier_invokes_critic_with_phase_diff(
    tmp_path: Path,
) -> None:
    """The runner dispatches role=``critic_drift_verifier`` with a prompt
    containing the phase spec, the diff, and the acceptance criteria."""
    from orchestrator.drift_verifier import run_drift_verifier

    adapter = StubAdapter(
        {
            "critic_drift_verifier": ok(
                "PHASE VERIFICATION:\n"
                "TASK 2.1: VERIFIED\n"
                "VERDICT: APPROVED\n"
            )
        }
    )
    orch = _OrchStub(adapter, tmp_path)
    phase = _phase()
    evidence_dir = tmp_path / ".autodev" / "evidence"

    verdict = await run_drift_verifier(
        orch=orch,
        phase=phase,
        evidence_dir=evidence_dir,
        diff_text="diff --git a/foo.py b/foo.py\n+def feature_x(): ...\n",
    )

    assert verdict.passed is True

    # The critic was invoked exactly once with the right role and a
    # DRIFT_VERIFY_CONTEXT block carrying the spec / diff / acceptance.
    assert adapter.count("critic_drift_verifier") == 1
    inv: AgentInvocation = adapter.calls[0]
    assert inv.role == "critic_drift_verifier"
    assert "DRIFT_VERIFY_CONTEXT" in inv.prompt
    assert "feature_x" in inv.prompt  # phase description
    assert "foo.py" in inv.prompt  # from the diff
    assert "feature_x exists" in inv.prompt  # acceptance criterion text


@pytest.mark.asyncio
async def test_drift_verifier_passes_when_no_drift_findings(
    tmp_path: Path,
) -> None:
    """An ``APPROVED`` verdict from the critic translates to ``passed=True``."""
    from orchestrator.drift_verifier import run_drift_verifier

    adapter = StubAdapter(
        {
            "critic_drift_verifier": ok(
                "## DRIFT REPORT\nUnplanned additions: none\n"
                "## PHASE VERDICT\nVERDICT: APPROVED\n"
            )
        }
    )
    orch = _OrchStub(adapter, tmp_path)
    verdict = await run_drift_verifier(
        orch=orch,
        phase=_phase(),
        evidence_dir=tmp_path / ".autodev" / "evidence",
        diff_text="",
    )
    assert verdict.passed is True
    assert verdict.drift_findings == []


@pytest.mark.asyncio
async def test_drift_verifier_fails_with_findings(tmp_path: Path) -> None:
    """A ``NEEDS_REVISION`` verdict surfaces parsed drift findings."""
    from orchestrator.drift_verifier import run_drift_verifier

    adapter = StubAdapter(
        {
            "critic_drift_verifier": ok(
                "PHASE VERIFICATION:\n"
                "TASK 2.1: DRIFTED\n"
                "  - Spec Alignment: DRIFTED — implemented feature_y not feature_x\n"
                "## DRIFT REPORT\n"
                "Unplanned additions: feature_y in foo.py\n"
                "Dropped tasks: 2.1 was not implemented as specified\n"
                "## PHASE VERDICT\n"
                "VERDICT: NEEDS_REVISION\n"
                "  - DRIFTED tasks: 2.1\n"
            )
        }
    )
    orch = _OrchStub(adapter, tmp_path)
    verdict = await run_drift_verifier(
        orch=orch,
        phase=_phase(),
        evidence_dir=tmp_path / ".autodev" / "evidence",
        diff_text="diff --git a/foo.py b/foo.py\n+def feature_y(): ...\n",
    )
    assert verdict.passed is False
    assert len(verdict.drift_findings) >= 1
    assert any("feature_y" in f for f in verdict.drift_findings) or any(
        "DRIFTED" in f for f in verdict.drift_findings
    )


@pytest.mark.asyncio
async def test_drift_verifier_evidence_persisted_to_evidence_dir(
    tmp_path: Path,
) -> None:
    """The runner writes ``evidence/{phase_id}-drift-verifier.json``."""
    from orchestrator.drift_verifier import run_drift_verifier

    adapter = StubAdapter(
        {
            "critic_drift_verifier": ok(
                "## PHASE VERDICT\nVERDICT: APPROVED\n"
            )
        }
    )
    orch = _OrchStub(adapter, tmp_path)
    evidence_dir = tmp_path / ".autodev" / "evidence"
    phase = _phase()

    verdict = await run_drift_verifier(
        orch=orch,
        phase=phase,
        evidence_dir=evidence_dir,
        diff_text="",
    )
    expected = evidence_dir / f"{phase.id}-drift-verifier.json"
    assert verdict.evidence_path == expected
    assert expected.exists()
    payload = json.loads(expected.read_text(encoding="utf-8"))
    assert payload["passed"] is True
    assert payload["phase_id"] == phase.id
    assert "raw_response" in payload


@pytest.mark.asyncio
async def test_drift_verifier_unparseable_response_falls_back_to_failure(
    tmp_path: Path,
) -> None:
    """When the critic emits a response without VERDICT lines, the runner
    treats it as a failure (skeptical default — absence of a clean
    APPROVAL means we cannot promote the phase)."""
    from orchestrator.drift_verifier import run_drift_verifier

    adapter = StubAdapter(
        {
            "critic_drift_verifier": ok(
                "I had trouble reading the files. The system was slow."
            )
        }
    )
    orch = _OrchStub(adapter, tmp_path)
    verdict = await run_drift_verifier(
        orch=orch,
        phase=_phase(),
        evidence_dir=tmp_path / ".autodev" / "evidence",
        diff_text="",
    )
    assert verdict.passed is False
    # Findings carry a parser-fallback marker for telemetry.
    assert verdict.drift_findings, "expected at least one fallback finding"


@pytest.mark.asyncio
async def test_drift_verifier_uses_phase_id_to_key_evidence(
    tmp_path: Path,
) -> None:
    """Evidence path includes the phase id verbatim (filesystem-safe)."""
    from orchestrator.drift_verifier import run_drift_verifier

    adapter = StubAdapter(
        {
            "critic_drift_verifier": ok(
                "## PHASE VERDICT\nVERDICT: APPROVED\n"
            )
        }
    )
    orch = _OrchStub(adapter, tmp_path)
    phase = _phase()
    phase.id = "phase-test/with-slashes"
    evidence_dir = tmp_path / ".autodev" / "evidence"

    verdict = await run_drift_verifier(
        orch=orch,
        phase=phase,
        evidence_dir=evidence_dir,
        diff_text="",
    )
    # Slash sanitized for filesystem safety.
    assert "/" not in verdict.evidence_path.name
