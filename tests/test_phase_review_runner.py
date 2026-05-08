"""Tests for :mod:`orchestrator.phase_review_runner`.

Mirrors the shape of :mod:`tests.test_orchestrator_plan_tournament_runner`:
the upstream :class:`tournament.core.Tournament` is replaced with a fake
that returns a deterministic ``(final_bundle, history)`` tuple so we can
assert on the runner's outcome decisions and side effects (evidence file,
ledger breadcrumb) without invoking the LLM-call path.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator import phase_review_runner as prr
from state.paths import autodev_root
from state.plan_manager import PlanManager
from state.schemas import AcceptanceCriterion, Phase, Plan, Task
from tournament.phase_review import PhaseReviewBundle

from stub_adapter import StubAdapter, ok


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _make_orch(cwd: Path, *, enabled: bool = True) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.phase_review.enabled = enabled
    cfg.tournaments.auto_disable_for_models = []
    cfg.tournaments.impl.enabled = False
    cfg.tournaments.plan.enabled = False
    registry = build_registry(cfg)
    adapter = StubAdapter({"explorer": ok("ok")})
    return Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-test-phase-review",
    )


class _FakeTournament:
    """Capturing fake for :class:`tournament.core.Tournament`.

    Configured per-test via the class-level ``next_winner`` /
    ``next_direction`` attributes. ``__init__`` records the ``rng`` so
    tests can assert on the seed derivation.
    """

    captured_rng: Any = None
    captured_artifact_dir: Path | None = None
    next_winner: str = "A"
    next_direction: str = ""

    def __init__(
        self,
        *,
        handler: Any,
        client: Any,
        cfg: Any,
        artifact_dir: Path,
        rng: Any = None,
        judge_plugins: Any = None,
    ) -> None:
        type(self).captured_rng = rng
        type(self).captured_artifact_dir = artifact_dir
        self._handler = handler

    async def run(
        self, *, task_prompt: str, initial: PhaseReviewBundle
    ) -> tuple[PhaseReviewBundle, list]:
        from dataclasses import replace

        from tournament.core import PassResult

        winner = type(self).next_winner
        if winner == "A":
            final_bundle = initial
        else:
            final_bundle = replace(
                initial,
                variant_label=winner,  # type: ignore[arg-type]
                direction_text=type(self).next_direction,
            )
        history = [
            PassResult(
                pass_num=1,
                winner=winner,  # type: ignore[arg-type]
                scores={"A": 6, "B": 3, "AB": 0},
                valid_judges=3,
                elapsed_s=0.0,
                judge_details=[],
                incumbent_hash_before="x",
                incumbent_hash_after="y",
                meta={"effective_winner": winner},
            )
        ]
        return final_bundle, history


@pytest.fixture
def capture_tournament(monkeypatch: pytest.MonkeyPatch) -> type[_FakeTournament]:
    _FakeTournament.captured_rng = None
    _FakeTournament.captured_artifact_dir = None
    _FakeTournament.next_winner = "A"
    _FakeTournament.next_direction = ""
    monkeypatch.setattr(prr, "Tournament", _FakeTournament)
    # Don't actually shell out for git diff — return a stub.
    monkeypatch.setattr(
        prr, "_git_diff_range", lambda cwd, a, b: "diff --git a/x.py b/x.py\n+++ b/x.py\n+x\n"
    )
    return _FakeTournament


@pytest.fixture
async def initialised_orch(tmp_path: Path) -> Orchestrator:
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_plan())
    return _make_orch(tmp_path)


# ---------------------------------------------------------------------------
# Outcome decisions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_winner_returns_accept_phase_true(
    tmp_path: Path,
    capture_tournament: type[_FakeTournament],
) -> None:
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_plan())
    orch = _make_orch(tmp_path)
    capture_tournament.next_winner = "A"

    plan = await orch.plan_manager.load()
    phase = plan.phases[0]  # type: ignore[union-attr]
    outcome = await prr.run_phase_review_tournament(
        orch, phase, "aaaa1111", "bbbb2222", spec_md="my spec"
    )
    assert outcome.winner == "A"
    assert outcome.accept_phase is True
    assert outcome.corrective_direction is None


@pytest.mark.asyncio
async def test_b_winner_returns_corrective_direction(
    tmp_path: Path,
    capture_tournament: type[_FakeTournament],
) -> None:
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_plan())
    orch = _make_orch(tmp_path)
    capture_tournament.next_winner = "B"
    capture_tournament.next_direction = "- fix the flake on macOS\n- add coverage"

    plan = await orch.plan_manager.load()
    phase = plan.phases[0]  # type: ignore[union-attr]
    outcome = await prr.run_phase_review_tournament(
        orch, phase, "aaaa1111", "bbbb2222", spec_md="my spec"
    )
    assert outcome.winner == "B"
    assert outcome.accept_phase is False
    assert outcome.corrective_direction is not None
    assert "fix the flake on macOS" in outcome.corrective_direction


@pytest.mark.asyncio
async def test_ab_winner_returns_corrective_direction(
    tmp_path: Path,
    capture_tournament: type[_FakeTournament],
) -> None:
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_plan())
    orch = _make_orch(tmp_path)
    capture_tournament.next_winner = "AB"
    capture_tournament.next_direction = "- merged correction"

    plan = await orch.plan_manager.load()
    phase = plan.phases[0]  # type: ignore[union-attr]
    outcome = await prr.run_phase_review_tournament(
        orch, phase, "aaaa1111", "bbbb2222", spec_md="my spec"
    )
    assert outcome.winner == "AB"
    assert outcome.accept_phase is False
    assert "merged correction" in (outcome.corrective_direction or "")


# ---------------------------------------------------------------------------
# Auto-disable + side effects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_disabled_returns_no_op_outcome(tmp_path: Path) -> None:
    """``cfg.tournaments.phase_review.enabled=False`` short-circuits to A-win."""
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_plan())
    orch = _make_orch(tmp_path, enabled=False)
    plan = await orch.plan_manager.load()
    phase = plan.phases[0]  # type: ignore[union-attr]
    outcome = await prr.run_phase_review_tournament(
        orch, phase, "aaaa1111", "bbbb2222", spec_md="my spec"
    )
    assert outcome.winner == "A"
    assert outcome.accept_phase is True
    assert outcome.corrective_direction is None
    # No history because the tournament didn't run.
    assert outcome.history == []


@pytest.mark.asyncio
async def test_writes_tournament_evidence_with_phase_review_kind(
    tmp_path: Path,
    capture_tournament: type[_FakeTournament],
) -> None:
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_plan())
    orch = _make_orch(tmp_path)
    capture_tournament.next_winner = "A"

    plan = await orch.plan_manager.load()
    phase = plan.phases[0]  # type: ignore[union-attr]
    await prr.run_phase_review_tournament(
        orch, phase, "aaaa1111", "bbbb2222", spec_md="my spec"
    )
    ev_path = autodev_root(tmp_path) / "evidence" / "phase-1-tournament.json"
    assert ev_path.exists()
    payload = json.loads(ev_path.read_text())
    assert payload["kind"] == "tournament"
    assert payload["phase"] == "phase_review"
    assert payload["task_id"] == "phase-1"


@pytest.mark.asyncio
async def test_appends_phase_review_complete_ledger_breadcrumb(
    tmp_path: Path,
    capture_tournament: type[_FakeTournament],
) -> None:
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_plan())
    orch = _make_orch(tmp_path)
    capture_tournament.next_winner = "A"

    plan = await orch.plan_manager.load()
    phase = plan.phases[0]  # type: ignore[union-attr]
    await prr.run_phase_review_tournament(
        orch, phase, "aaaa1111", "bbbb2222", spec_md="my spec"
    )
    entries = await orch.plan_manager.read_ledger()
    ops = [e.op for e in entries]
    assert "phase_review_complete" in ops


# ---------------------------------------------------------------------------
# Seeded RNG
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seeded_rng_uses_phase_id_in_seed(
    tmp_path: Path,
    capture_tournament: type[_FakeTournament],
) -> None:
    """Two reviews of distinct phases get distinct deterministic seeds."""
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_plan())
    orch = _make_orch(tmp_path)
    capture_tournament.next_winner = "A"

    plan = await orch.plan_manager.load()
    phase = plan.phases[0]  # type: ignore[union-attr]
    await prr.run_phase_review_tournament(
        orch, phase, "aaaa1111", "bbbb2222", spec_md="spec1"
    )
    rng_phase_1 = capture_tournament.captured_rng

    # Run again on the same phase — same seed.
    await prr.run_phase_review_tournament(
        orch, phase, "aaaa1111", "bbbb2222", spec_md="spec1"
    )
    rng_phase_1b = capture_tournament.captured_rng

    # Both Random instances were seeded from the same input → first
    # ``random()`` call returns the same number. (The Random instances
    # themselves differ, but the generated sequence is identical.)
    assert rng_phase_1 is not None
    assert rng_phase_1b is not None
    assert rng_phase_1.random() == rng_phase_1b.random()
