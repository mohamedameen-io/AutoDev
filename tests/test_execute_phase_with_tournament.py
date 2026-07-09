"""Tests for execute_phase with impl tournament wired in (Phase 7)."""

from __future__ import annotations

import datetime as _dt
import subprocess
from pathlib import Path

import pytest

from adapters.types import AgentResult
from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator import impl_tournament_runner as itr
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
    return Plan(
        plan_id="p-exec-t7",
        spec_hash="d",
        phases=[
            Phase(
                id="1",
                title="Work",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="Add foo",
                        description="Implement foo()",
                        files=["foo.py"],
                        acceptance=[
                            AcceptanceCriterion(id="ac-1", description="tests pass"),
                        ],
                    )
                ],
            )
        ],
        created_at=_iso(),
        updated_at=_iso(),
    )


def _coder_ok() -> AgentResult:
    return AgentResult(
        success=True,
        text="wrote foo",
        diff=(
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -0,0 +1 @@\n"
            "+def foo(): pass\n"
        ),
        files_changed=[Path("foo.py")],
        duration_s=0.1,
    )


def _reviewer_ok() -> AgentResult:
    return ok("APPROVED\n- clean")


def _test_ok() -> AgentResult:
    return ok("ran pytest\nRESULTS: passed=3 failed=0 total=3")


async def _make_orch(
    cwd: Path,
    adapter: StubAdapter,
    *,
    impl_enabled: bool = True,
    judge_model: str = "sonnet",
    auto_disable: list[str] | None = None,
) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = impl_enabled
    cfg.tournaments.impl.num_judges = 1
    cfg.tournaments.impl.convergence_k = 1
    cfg.tournaments.impl.max_rounds = 2
    cfg.tournaments.auto_disable_for_models = auto_disable or []
    cfg.agents["judge"].model = judge_model
    registry = build_registry(cfg)
    orch = Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-exec-t7",
    )
    await orch.plan_manager.init_plan(_mk_plan())
    return orch


@pytest.mark.asyncio
async def test_execute_with_impl_tournament_disabled_completes(
    tmp_path: Path,
) -> None:
    """With impl tournament disabled, execute still completes normally."""
    adapter = StubAdapter(
        {
            "developer": _coder_ok(),
            "reviewer": _reviewer_ok(),
            "test_engineer": _test_ok(),
        }
    )
    orch = await _make_orch(tmp_path, adapter, impl_enabled=False)
    tasks = await orch.execute()
    assert len(tasks) == 1
    assert tasks[0].status == "complete"


@pytest.mark.asyncio
async def test_execute_with_impl_tournament_auto_disabled_completes(
    tmp_path: Path,
) -> None:
    """With opus judge + auto_disable=["opus"], tournament is skipped but task completes."""
    adapter = StubAdapter(
        {
            "developer": _coder_ok(),
            "reviewer": _reviewer_ok(),
            "test_engineer": _test_ok(),
        }
    )
    orch = await _make_orch(
        tmp_path,
        adapter,
        impl_enabled=True,
        judge_model="opus",
        auto_disable=["opus"],
    )
    tasks = await orch.execute()
    assert len(tasks) == 1
    assert tasks[0].status == "complete"
    # No tournament evidence written.
    ev_path = tmp_path / ".autodev" / "evidence" / "1.1-tournament.json"
    assert not ev_path.exists()


@pytest.mark.asyncio
async def test_execute_with_impl_tournament_error_still_completes(
    tmp_path: Path,
) -> None:
    """If the impl tournament raises, the task still completes (error is swallowed)."""
    # We trigger the tournament by enabling it with a non-opus model.
    # The tournament will fail because there's no real git repo for worktrees,
    # but the execute phase should catch the error and continue.
    adapter = StubAdapter(
        {
            "developer": _coder_ok(),
            "reviewer": _reviewer_ok(),
            "test_engineer": _test_ok(),
        }
    )
    orch = await _make_orch(
        tmp_path,
        adapter,
        impl_enabled=True,
        judge_model="sonnet",
        auto_disable=[],
    )
    tasks = await orch.execute()
    # Task should still complete even if tournament errors.
    assert len(tasks) == 1
    assert tasks[0].status == "complete"


@pytest.mark.asyncio
async def test_execute_with_impl_tournament_disabled_flag(
    tmp_path: Path,
) -> None:
    """``disable_impl_tournament=True`` skips tournament even when cfg.impl.enabled."""
    adapter = StubAdapter(
        {
            "developer": _coder_ok(),
            "reviewer": _reviewer_ok(),
            "test_engineer": _test_ok(),
        }
    )
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = True
    cfg.tournaments.impl.num_judges = 1
    cfg.tournaments.impl.convergence_k = 1
    cfg.tournaments.impl.max_rounds = 2
    cfg.tournaments.auto_disable_for_models = []
    cfg.agents["judge"].model = "sonnet"
    registry = build_registry(cfg)
    orch = Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-exec-t7-disabled",
        disable_impl_tournament=True,
    )
    await orch.plan_manager.init_plan(_mk_plan())
    tasks = await orch.execute()
    assert len(tasks) == 1
    assert tasks[0].status == "complete"
    # No tournament evidence.
    ev_path = tmp_path / ".autodev" / "evidence" / "1.1-tournament.json"
    assert not ev_path.exists()


# ── Code-review finding (must-fix companion): wall-budget breach ────────


def _git_init(path: Path) -> None:
    """Initialize a minimal git repo at *path* with one commit.

    Needed so the impl tournament's OWN worktree creation succeeds
    normally for a couple of real passes before the fake-clock wall-budget
    breach fires deterministically — as opposed to
    ``test_execute_with_impl_tournament_error_still_completes`` above,
    which relies on an INCIDENTAL (non-git-repo) worktree failure and
    doesn't pin the wall-budget breach specifically.
    """
    subprocess.run(["git", "init"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(path), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(path), check=True, capture_output=True,
    )
    (path / "README.md").write_text("init")
    subprocess.run(["git", "add", "."], cwd=str(path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=str(path), check=True, capture_output=True,
    )


class _FakeClock:
    """Monotonic-shaped fake clock that advances a fixed step per read.

    Mirrors ``tests.test_tournament_wall_budget._FakeClock`` /
    ``tests.test_impl_tournament_wall_budget._FakeClock``.
    """

    def __init__(self, step: float = 10.0, start: float = 1000.0) -> None:
        self._t = start
        self._step = step

    def __call__(self) -> float:
        v = self._t
        self._t += self._step
        return v


@pytest.mark.asyncio
async def test_execute_with_impl_tournament_wall_budget_breach_still_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drives a REAL wall-budget breach inside the impl tournament through
    the FULL ``execute_phase`` task pipeline (not ``run_impl_tournament`` in
    isolation), and confirms:

      1. ``execute_phase.py``'s existing ``except Exception`` around the
         impl-tournament call still swallows the re-raised
         ``TournamentError`` gracefully — the task completes rather than
         the whole ``execute()`` call blowing up.
      2. The task completes on the PRE-tournament basis: no
         ``TournamentEvidence`` was written (the breach fires inside
         ``run_impl_tournament``'s try/except/finally, BEFORE the function
         ever reaches its evidence-write step) — so nothing from a
         tournament refinement (which never succeeded) leaks in.
      3. The new ``impl_phase_wall_budget_exceeded`` ledger op IS present.
         This is the assertion that would have FAILED pre-fix: the real
         shared engine raised carrying the WRONG (hardcoded)
         ``plan_phase_wall_budget_exceeded`` marker, so the runner's guard
         never matched and the op was never emitted — exactly the gap
         code review found.

    Uses the same "monkeypatch the ``TournamentConfig`` name in the
    runner's module namespace to inject a fake clock" technique as
    ``tests.test_impl_tournament_wall_budget`` — ``ImplTournament`` itself
    is never touched, so this exercises the REAL engine end-to-end.
    """
    _git_init(tmp_path)
    adapter = StubAdapter(
        {
            "developer": _coder_ok(),
            "reviewer": _reviewer_ok(),
            "test_engineer": _test_ok(),
            "critic_t": ok("- nit"),
            "architect_b": ok("- minor fix"),
            "synthesizer": ok("- synthesize both"),
            "judge": ok("Good work.\n\nRANKING: 1, 2, 3"),
        }
    )
    orch = await _make_orch(
        tmp_path, adapter, impl_enabled=True, judge_model="sonnet"
    )
    orch.cfg.guardrails.impl_phase_wall_budget_s = 25.0
    # Prevent premature convergence / runaway-detector early-stops so the
    # wall budget is unambiguously the ONLY early-stop cause (mirrors the
    # core-level test in test_tournament_wall_budget.py).
    orch.cfg.tournaments.impl.convergence_k = 10
    orch.cfg.tournaments.impl.max_rounds = 50
    orch.cfg.tournaments.impl.score_stability_window = None
    orch.cfg.tournaments.impl.score_stability_max_delta = None
    orch.cfg.tournaments.impl.winner_stability_window = None

    clock = _FakeClock(step=10.0)

    class _FakeClockTournamentConfig(itr.TournamentConfig):  # type: ignore[misc]
        def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            kwargs.setdefault("clock", clock)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(itr, "TournamentConfig", _FakeClockTournamentConfig)

    tasks = await orch.execute()

    assert len(tasks) == 1
    assert tasks[0].status == "complete"

    ev_path = tmp_path / ".autodev" / "evidence" / "1.1-tournament.json"
    assert not ev_path.exists(), (
        "no TournamentEvidence should be written — the breach fires before "
        "run_impl_tournament reaches its evidence-write step"
    )

    entries = await orch.plan_manager.read_ledger()
    ops = [e.op for e in entries]
    assert "impl_phase_wall_budget_exceeded" in ops, (
        f"expected the impl wall-budget ledger op from the swallowed "
        f"breach; got ops={ops}"
    )
