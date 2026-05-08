"""Integration test for drift-verifier wiring into phase_review_runner.

When :func:`run_phase_review_tournament` returns an A-winner outcome
(accept_phase=True), the runner now invokes
:func:`orchestrator.drift_verifier.run_drift_verifier` as a final-defense
gate. If the drift verifier reports findings, the outcome is overridden
to ``accept_phase=False`` with a ``corrective_direction`` derived from
the drift findings.

Test strategy: monkeypatch :class:`tournament.core.Tournament` (mirrors
the existing test_phase_review_runner pattern), force an A winner, and
toggle the drift critic via the StubAdapter to surface or suppress
drift findings.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator import phase_review_runner as prr
from state.plan_manager import PlanManager
from state.schemas import AcceptanceCriterion, Phase, Plan, Task
from tournament.phase_review import PhaseReviewBundle

from stub_adapter import StubAdapter, ok


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan() -> Plan:
    return Plan(
        plan_id="p-test",
        spec_hash="0123456789abcdef",
        phases=[
            Phase(
                id="1",
                title="Investigate",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="t",
                        description="d",
                        complexity="medium",
                        status="complete",
                    ),
                ],
                acceptance=[
                    AcceptanceCriterion(
                        id="ph-ac-1", description="all tests pass"
                    ),
                ],
                baseline_commit="aaaa1111",
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
        complexity="medium",
    )


def _make_orch(cwd: Path, adapter: StubAdapter) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.phase_review.enabled = True
    # v0.16.0: opt in to the drift-verifier final-defense gate.
    cfg.tournaments.phase_review.drift_verifier_enabled = True
    cfg.tournaments.auto_disable_for_models = []
    cfg.tournaments.impl.enabled = False
    cfg.tournaments.plan.enabled = False
    registry = build_registry(cfg)
    return Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-test-drift",
    )


class _FakeTournamentAWin:
    """Tournament fake that always returns an A winner."""

    def __init__(self, **kwargs: Any) -> None:
        pass

    async def run(
        self, *, task_prompt: str, initial: PhaseReviewBundle
    ) -> tuple[PhaseReviewBundle, list]:
        from tournament.core import PassResult

        return initial, [
            PassResult(
                pass_num=1,
                winner="A",
                scores={"A": 6, "B": 3, "AB": 0},
                valid_judges=3,
                elapsed_s=0.0,
                judge_details=[],
                incumbent_hash_before="x",
                incumbent_hash_after="x",
                meta={"effective_winner": "A"},
            )
        ]


@pytest.fixture
def capture_tournament(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prr, "Tournament", _FakeTournamentAWin)
    monkeypatch.setattr(
        prr,
        "_git_diff_range",
        lambda cwd, a, b: "diff --git a/x.py b/x.py\n+++ b/x.py\n+x\n",
    )


@pytest.mark.asyncio
async def test_phase_review_with_no_drift_passes_through(
    tmp_path: Path, capture_tournament: None
) -> None:
    """A-winner + no drift findings → accept_phase remains True."""
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_plan())
    adapter = StubAdapter(
        {
            "critic_drift_verifier": ok(
                "## PHASE VERDICT\nVERDICT: APPROVED\n"
            )
        }
    )
    orch = _make_orch(tmp_path, adapter)
    plan = await orch.plan_manager.load()
    phase = plan.phases[0]  # type: ignore[union-attr]

    outcome = await prr.run_phase_review_tournament(
        orch, phase, "aaaa1111", "bbbb2222", spec_md="my spec"
    )

    assert outcome.winner == "A"
    assert outcome.accept_phase is True
    assert outcome.corrective_direction is None


@pytest.mark.asyncio
async def test_phase_review_with_drift_detected_overrides_to_corrective_required(
    tmp_path: Path, capture_tournament: None
) -> None:
    """A-winner + drift findings → outcome flipped to accept_phase=False
    with a corrective_direction listing the drift findings."""
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_plan())
    adapter = StubAdapter(
        {
            "critic_drift_verifier": ok(
                "PHASE VERIFICATION:\n"
                "TASK 1.1: DRIFTED\n"
                "  - Spec Alignment: DRIFTED — implemented Y not X\n"
                "## PHASE VERDICT\nVERDICT: NEEDS_REVISION\n"
                "  - DRIFTED tasks: 1.1\n"
            )
        }
    )
    orch = _make_orch(tmp_path, adapter)
    plan = await orch.plan_manager.load()
    phase = plan.phases[0]  # type: ignore[union-attr]

    outcome = await prr.run_phase_review_tournament(
        orch, phase, "aaaa1111", "bbbb2222", spec_md="my spec"
    )

    # Tournament said A; drift verifier overrode to corrective.
    assert outcome.accept_phase is False
    assert outcome.corrective_direction is not None
    # Direction text references the drift findings.
    assert "drift" in outcome.corrective_direction.lower() or "1.1" in outcome.corrective_direction


@pytest.mark.asyncio
async def test_phase_review_b_winner_skips_drift_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-A winners already failed phase-review; drift-verifier doesn't
    need to run a second time."""

    class _FakeTournamentBWin:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def run(
            self, *, task_prompt: str, initial: PhaseReviewBundle
        ) -> tuple[PhaseReviewBundle, list]:
            from dataclasses import replace

            from tournament.core import PassResult

            final_bundle = replace(
                initial, variant_label="B", direction_text="- fix this"
            )
            return final_bundle, [
                PassResult(
                    pass_num=1,
                    winner="B",
                    scores={"A": 3, "B": 6, "AB": 0},
                    valid_judges=3,
                    elapsed_s=0.0,
                    judge_details=[],
                    incumbent_hash_before="x",
                    incumbent_hash_after="y",
                    meta={"effective_winner": "B"},
                )
            ]

    monkeypatch.setattr(prr, "Tournament", _FakeTournamentBWin)
    monkeypatch.setattr(
        prr,
        "_git_diff_range",
        lambda cwd, a, b: "diff --git a/x.py b/x.py\n+++ b/x.py\n+x\n",
    )

    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_plan())
    adapter = StubAdapter(
        {
            "critic_drift_verifier": ok(
                "## PHASE VERDICT\nVERDICT: APPROVED\n"
            )
        }
    )
    orch = _make_orch(tmp_path, adapter)
    plan = await orch.plan_manager.load()
    phase = plan.phases[0]  # type: ignore[union-attr]

    outcome = await prr.run_phase_review_tournament(
        orch, phase, "aaaa1111", "bbbb2222", spec_md="my spec"
    )

    # B winner stays B — corrective from tournament, not from drift.
    assert outcome.winner == "B"
    assert outcome.accept_phase is False
    # Drift verifier was not invoked (count==0): the runner short-
    # circuits the drift check on non-A winners.
    assert adapter.count("critic_drift_verifier") == 0
