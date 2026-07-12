"""WS1 integration + config pins: ``test_engineer`` turn-budget exhaustion is
attributed to ``turn_budget_exhausted`` (not ``capture_failed``) and drives a
correct hard-fail — never a silent soft-pass.

Covers the cross-cutting behaviour that the unit tests (classifier / schema /
adapter / prompt) cannot: a real ``run_execute_phase`` where the stubbed
``test_engineer`` runs out of turns twice, and the task ends ``blocked`` with
the new diagnosis, the dispatch subtype persisted on ``TestEvidence``, and NO
``soft_passed`` marker.

Explicit WS1 risk (intended): a run that would previously have been silently
soft-passed now correctly hard-fails toward ``blocked`` / the resolver.
"""

from __future__ import annotations

import datetime as _dt
import subprocess
from pathlib import Path
from typing import Any

import pytest

from adapters.types import AgentResult
from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator import execute_phase as ep
from state.evidence import read_evidence
from state.plan_manager import PlanManager
from state.schemas import AcceptanceCriterion, Phase, Plan, Task, TestEvidence

from stub_adapter import StubAdapter, fail, ok


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan(plan_id: str = "p-ws1-tb") -> Plan:
    return Plan(
        plan_id=plan_id,
        spec_hash="0123456789abcdef",
        phases=[
            Phase(
                id="1",
                title="Work",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="t1",
                        description="d1",
                        files=["math_utils.py"],
                        complexity="medium",
                        acceptance=[
                            AcceptanceCriterion(id="ac-1", description="ok"),
                        ],
                    ),
                ],
                acceptance=[AcceptanceCriterion(id="ph-1", description="ok")],
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
        complexity="medium",
    )


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=str(repo), check=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True)
    (repo / "math_utils.py").write_text("def add(a, b):\n    return a + b\n")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(repo), check=True)


def _coder_diff(variant: str) -> AgentResult:
    return AgentResult(
        success=True,
        text=f"wrote ({variant})",
        diff=(
            "diff --git a/math_utils.py b/math_utils.py\n"
            "--- a/math_utils.py\n"
            "+++ b/math_utils.py\n"
            "@@ -1,2 +1,3 @@\n"
            " def add(a, b):\n"
            "     return a + b\n"
            f"+# {variant}\n"
        ),
        files_changed=[Path("math_utils.py")],
        duration_s=0.1,
    )


def _make_cfg() -> Any:
    cfg = default_config()
    cfg.tournaments.phase_review.enabled = False
    cfg.tournaments.impl.enabled = False
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.auto_disable_for_models = []
    cfg.qa_retry_min_interval_s = 0.0
    cfg.qa_retry_limit = 1
    # Reach the test_engineer step (Step 5) without the coarse QA gates
    # (which would run pytest against a fixture repo with no tests).
    cfg.qa_gates.syntax_check = False
    cfg.qa_gates.lint = False
    cfg.qa_gates.build_check = False
    cfg.qa_gates.test_runner = False
    cfg.qa_gates.secretscan = False
    return cfg


async def _build_orch(repo: Path, adapter: StubAdapter, *, session: str) -> Orchestrator:
    cfg = _make_cfg()
    registry = build_registry(cfg)
    pm = PlanManager(repo, session_id=f"{session}-init")
    await pm.init_plan(_mk_plan())
    return Orchestrator(
        cwd=repo,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id=f"{session}-exec",
    )


# ---------------------------------------------------------------------------
# Config / breaker default (1c).
# ---------------------------------------------------------------------------


def test_default_test_diag_breaker_includes_turn_budget() -> None:
    """WS1 1c: the shipped default arms the cross-task breaker for BOTH
    ``capture_failed`` and ``turn_budget_exhausted`` so extracting the latter
    out of the ``capture_failed`` bucket does not quietly lose coverage."""
    cfg = default_config()
    assert cfg.test_diag_breaker_diagnoses == [
        "capture_failed",
        "turn_budget_exhausted",
    ]


def test_breaker_counts_turn_budget_when_configured_with_default() -> None:
    """A breaker built from the shipped default set actually counts
    ``turn_budget_exhausted`` toward its trip threshold (default 3)."""
    from orchestrator.circuit_breaker import InfraFailureCircuitBreaker

    base = _dt.datetime(2026, 7, 10, 12, 0, 0, tzinfo=_dt.timezone.utc)
    cb = InfraFailureCircuitBreaker(
        test_diag_diagnoses=frozenset(default_config().test_diag_breaker_diagnoses)
    )
    # Below threshold → no backoff yet.
    cb.record_test_diagnosis("t1", "turn_budget_exhausted", base)
    cb.record_test_diagnosis("t2", "turn_budget_exhausted", base)
    assert cb.next_backoff_s_for_test_diag() is None
    # Threshold cross (3rd in window) → the breaker returns a backoff, proving
    # the new diagnosis is counted (a diagnosis NOT in the set is ignored).
    cb.record_test_diagnosis("t3", "turn_budget_exhausted", base)
    assert cb.next_backoff_s_for_test_diag() is not None


def test_breaker_ignores_turn_budget_when_not_in_set() -> None:
    """Broken-control: a breaker whose set is the pre-WS1 ``["capture_failed"]``
    does NOT count ``turn_budget_exhausted`` — pinning that the default change
    (not the breaker code) is what wires coverage."""
    from orchestrator.circuit_breaker import InfraFailureCircuitBreaker

    base = _dt.datetime(2026, 7, 10, 12, 0, 0, tzinfo=_dt.timezone.utc)
    cb = InfraFailureCircuitBreaker(test_diag_diagnoses=frozenset({"capture_failed"}))
    for i in range(5):
        cb.record_test_diagnosis(f"t{i}", "turn_budget_exhausted", base)
    assert cb.next_backoff_s_for_test_diag() is None


# ---------------------------------------------------------------------------
# Drift guard: the turn-exhaustion subtype set is intentionally DUPLICATED —
# the classifier must stay a pure leaf module, free of the orchestrator/adapter
# dependency tree. Guard against the two copies silently diverging.
# ---------------------------------------------------------------------------


def test_turn_exhaustion_subtype_sets_do_not_drift() -> None:
    """``test_result_classifier._TURN_BUDGET_EXHAUSTION_SUBTYPES`` must equal
    ``execute_phase._TURN_EXHAUSTION_SUBTYPES``.

    Silent divergence would re-classify a turn-exhaustion subtype as
    ``capture_failed`` and re-open the silent-soft-pass hole WS1 closes. The
    duplication is deliberate (keep the classifier importable without pulling
    the adapter/orchestrator tree); this test is the tripwire that keeps the
    two copies honest.
    """
    from orchestrator import test_result_classifier as trc

    assert (
        trc._TURN_BUDGET_EXHAUSTION_SUBTYPES == ep._TURN_EXHAUSTION_SUBTYPES
    ), (
        "turn-exhaustion subtype sets have drifted between the classifier and "
        "execute_phase — a turn-exhausted dispatch would be misclassified as "
        "capture_failed. Keep the two frozensets in lockstep."
    )


# ---------------------------------------------------------------------------
# Orchestration (1a + 1b + 1c end-to-end).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_exhausted_test_engineer_never_soft_passed(
    tmp_path: Path,
) -> None:
    """A ``test_engineer`` that exhausts its turn budget twice is correctly
    DIAGNOSED ``turn_budget_exhausted`` (never silently soft-passed) with the
    dispatch subtype persisted on ``TestEvidence`` for forensics, and routes to
    ``block_task`` with ``TEST_DIAGNOSIS_HARDFAIL``.

    Terminal outcome (WS3 widening — SPEC-MANDATED): the winning diff was
    reviewer-APPROVED and applies cleanly to ``main``, so WS3's validated-patch
    recovery — which fires on ANY terminal block incl. ``test_diagnosis_hardfail``
    /turn-budget, the exact spec headline class — COMPLETES the task instead of
    discarding it, stamped with an explicit ``needs_human_review`` /
    needs-verification marker. That is NOT a silent soft-pass (WS-1's real
    guarantee): the diagnosis, retry contract, and forensics below are all
    intact, and the completion is loudly flagged for human verification rather
    than certified as a clean test-verified pass.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    exhausted = fail("Reached maximum number of turns", subtype="error_max_turns")
    adapter = StubAdapter(
        {
            "explorer": ok("ok"),
            # Fresh diff per dispatch (the retry re-runs the developer).
            "developer": [_coder_diff(f"v{i}") for i in range(1, 6)],
            "reviewer": ok("APPROVED\n- ok"),
            # Repeats (StubAdapter reuses the last list entry): turn-exhausted
            # every time it is asked to test.
            "test_engineer": exhausted,
        }
    )
    orch = await _build_orch(repo, adapter, session="ws1tb")

    tasks = await ep.run_execute_phase(orch)
    assert len(tasks) == 1
    final = tasks[0]

    # Terminal outcome: WS3's validated-patch recovery COMPLETES the task
    # (spec-mandated for a reviewer-APPROVED, cleanly-appliable diff blocked on
    # TEST_DIAGNOSIS_HARDFAIL), stamped with an explicit needs-verification
    # marker — NOT silently soft-passed, and NOT certified as a clean pass.
    assert final.status == "complete", (
        f"expected complete (WS3 recovery), got {final.status} "
        f"(blocked_reason={final.blocked_reason})"
    )
    assert final.metadata.get("needs_human_review") is True, (
        "a recovered-but-unverified completion must be loudly flagged "
        "needs_human_review (never a silent pass); "
        f"metadata={final.metadata!r}"
    )

    # Retried once, then hard-failed on the second occurrence. With
    # ``qa_retry_limit = 1`` the exact contract is 2 dispatches — pinning
    # ``== 2`` (not ``>= 2``) also catches an over-retry regression.
    assert adapter.count("test_engineer") == 2, (
        f"expected exactly 2 test_engineer dispatches (retry-then-hard-fail); "
        f"got {adapter.count('test_engineer')}"
    )

    # Durable evidence carries the diagnosis + the dispatch-layer subtype, and
    # is NEVER marked soft-passed (the test gate correctly hard-failed; the
    # completion is WS3 recovery, a DISTINCT explicitly-flagged mechanism).
    ev = await read_evidence(repo, "1.1", "test")
    assert isinstance(ev, TestEvidence)
    assert ev.diagnosis == "turn_budget_exhausted"
    assert ev.agent_subtype == "error_max_turns"
    assert not ev.soft_passed, (
        "a turn-exhausted run must never be silently soft-passed "
        f"(soft_passed={ev.soft_passed!r})"
    )
