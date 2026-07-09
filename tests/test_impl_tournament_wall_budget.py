"""Task 1 (wall-budget fix, sibling of F-7): impl-tournament wall-clock
budget WIRING (deterministic, NO live claude).

The underlying cumulative-wall-clock CHECK is the shared engine logic in
:meth:`tournament.core.Tournament.run` (``ImplTournament`` subclasses
``Tournament`` and only overrides ``run_pass``, so the check is inherited
unchanged) — that engine behavior is already pinned by
``tests/test_tournament_wall_budget.py``'s core-level tests. This file only
pins the impl-tournament-runner-specific WIRING, mirroring that file's
runner-level tests:

  * ``cfg.guardrails.impl_phase_wall_budget_s`` is read and threaded into
    the ``TournamentConfig.wall_budget_s`` constructed by
    :func:`orchestrator.impl_tournament_runner.run_impl_tournament`.
  * Default (unset) threads through as ``None`` — legacy byte-identical.
  * A ``TournamentError`` carrying the ``impl_phase_wall_budget_exceeded``
    marker emits that ledger op (with attribution payload) and re-raises.
  * A NON-budget ``TournamentError`` (e.g. survivor floor) does NOT emit
    that ledger op.
"""

from __future__ import annotations

import datetime as _dt
import random
import subprocess
from pathlib import Path

import pytest

from adapters.types import AgentResult
from config.defaults import default_config
from agents import build_registry
from errors import TournamentError
from orchestrator import Orchestrator
from orchestrator import impl_tournament_runner as itr
from orchestrator.impl_tournament_runner import run_impl_tournament
from state.schemas import Phase, Plan, Task
from tournament import ImplBundle, ImplContentHandler, ImplTournament, StubLLMClient

from stub_adapter import StubAdapter


# The greppable marker the loud failure carries — also the ledger op name.
_WALL_BUDGET_MARKER = "impl_phase_wall_budget_exceeded"


def _git_init(path: Path) -> None:
    """Initialize a minimal git repo at *path* with one commit.

    Mirrors ``tests.test_impl_tournament_runner._git_init`` — the impl
    runner builds a ``WorktreeManager`` rooted at ``orch.cwd`` even when
    ``ImplTournament`` itself is monkeypatched out, so a real repo must
    exist on disk.
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


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


INITIAL_BUNDLE = ImplBundle(
    task_id="1.1",
    task_description="Add foo()",
    diff="+def foo(): pass",
    files_changed=["foo.py"],
    tests_passed=3,
    tests_failed=0,
    tests_total=3,
    test_output_excerpt="3 passed",
)


def _mk_plan() -> Plan:
    return Plan(
        plan_id="p-impl-wall-budget-test",
        spec_hash="h",
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
                    )
                ],
            )
        ],
        created_at=_iso(),
        updated_at=_iso(),
    )


def _make_orch(
    cwd: Path,
    adapter: StubAdapter,
    *,
    impl_phase_wall_budget_s: float | None,
) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.impl.enabled = True
    cfg.tournaments.impl.num_judges = 1
    cfg.tournaments.impl.convergence_k = 1
    cfg.tournaments.impl.max_rounds = 3
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.auto_disable_for_models = []
    cfg.tournaments.impl.auto_disable_for_models = []
    cfg.guardrails.impl_phase_wall_budget_s = impl_phase_wall_budget_s
    registry = build_registry(cfg)
    return Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-impl-wall-budget-test",
    )


async def _setup(
    tmp_path: Path, *, impl_phase_wall_budget_s: float | None
) -> tuple[Orchestrator, Task]:
    _git_init(tmp_path)
    adapter = StubAdapter({})
    orch = _make_orch(
        tmp_path, adapter, impl_phase_wall_budget_s=impl_phase_wall_budget_s
    )
    await orch.plan_manager.init_plan(_mk_plan())
    task = await orch.plan_manager.get_task("1.1")
    assert task is not None
    return orch, task


class _Capture:
    """Stand-in ``ImplTournament`` that captures the constructed cfg and
    converges instantly (returns the initial bundle unchanged, no history).
    """

    captured_cfg = None

    def __init__(self, *, cfg=None, **_kwargs) -> None:  # type: ignore[no-untyped-def]
        type(self).captured_cfg = cfg

    async def run(self, *, task_prompt: str, initial):  # type: ignore[no-untyped-def]
        return initial, []


class _BudgetBreachTournament:
    """Stand-in ``ImplTournament`` whose ``run`` raises the budget-breach
    error.

    Mirrors the real loop's behavior at the breach: it raises a
    ``TournamentError`` carrying the ``impl_phase_wall_budget_exceeded``
    marker (the real loop also writes ``final_output.md`` first; that's
    covered by the shared core-level test in ``test_tournament_wall_budget.py``).
    """

    def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
        pass

    async def run(self, *, task_prompt: str, initial):  # type: ignore[no-untyped-def]
        raise TournamentError(
            "impl_phase_wall_budget_exceeded: tournament wall-clock budget of "
            "5.0s exceeded after 6.0s (2 pass(es) completed); stopping LOUD."
        )


class _SurvivorFloorTournament:
    """Stand-in ``ImplTournament`` raising a NON-budget ``TournamentError``."""

    def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
        pass

    async def run(self, *, task_prompt: str, initial):  # type: ignore[no-untyped-def]
        raise TournamentError("only 1 of 3 branches succeeded; survivor floor is 2")


# ── Runner-level wiring: config threading ───────────────────────────────


@pytest.mark.asyncio
async def test_runner_threads_budget_into_tournament_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run_impl_tournament`` threads ``cfg.guardrails.impl_phase_wall_budget_s``
    into the constructed ``TournamentConfig``."""
    orch, task = await _setup(tmp_path, impl_phase_wall_budget_s=123.0)
    monkeypatch.setattr(itr, "ImplTournament", _Capture)

    await run_impl_tournament(orch, task, INITIAL_BUNDLE)

    assert _Capture.captured_cfg is not None
    assert _Capture.captured_cfg.wall_budget_s == 123.0


@pytest.mark.asyncio
async def test_runner_default_none_threads_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default (unset) budget threads ``None`` into the config — OFF, the
    byte-identical-legacy path."""
    orch, task = await _setup(tmp_path, impl_phase_wall_budget_s=None)
    monkeypatch.setattr(itr, "ImplTournament", _Capture)

    await run_impl_tournament(orch, task, INITIAL_BUNDLE)

    assert _Capture.captured_cfg is not None
    assert _Capture.captured_cfg.wall_budget_s is None


# ── Runner-level wiring: the LOUD, attributable ledger op + re-raise ────


@pytest.mark.asyncio
async def test_runner_emits_ledger_op_and_reraises_on_breach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A budget-breach ``TournamentError`` → LOUD ledger op + re-raise.

    The re-raise is essential: callers of ``run_impl_tournament`` (e.g.
    ``execute_phase``) have their own recovery/fallback around it. The
    ledger op is the greppable, attributable reason that replaces the
    opaque external timeout.
    """
    orch, task = await _setup(tmp_path, impl_phase_wall_budget_s=5.0)
    monkeypatch.setattr(itr, "ImplTournament", _BudgetBreachTournament)

    with pytest.raises(TournamentError) as exc_info:
        await run_impl_tournament(orch, task, INITIAL_BUNDLE)
    assert _WALL_BUDGET_MARKER in str(exc_info.value)

    entries = await orch.plan_manager.read_ledger()
    ops = [e.op for e in entries]
    assert _WALL_BUDGET_MARKER in ops, (
        f"expected the loud ledger op to be appended; got ops={ops}"
    )
    # The emitted op's payload carries the attribution figures.
    breach = next(e for e in entries if e.op == _WALL_BUDGET_MARKER)
    assert breach.payload["budget_s"] == 5.0
    assert breach.payload["task_id"] == "1.1"
    assert "tournament_id" in breach.payload
    assert _WALL_BUDGET_MARKER in breach.payload["reason"]


@pytest.mark.asyncio
async def test_runner_does_not_emit_op_for_non_budget_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A NON-budget ``TournamentError`` (survivor floor) re-raises WITHOUT
    the wall-budget op — we only annotate the specific breach."""
    orch, task = await _setup(tmp_path, impl_phase_wall_budget_s=5.0)
    monkeypatch.setattr(itr, "ImplTournament", _SurvivorFloorTournament)

    with pytest.raises(TournamentError) as exc_info:
        await run_impl_tournament(orch, task, INITIAL_BUNDLE)
    assert _WALL_BUDGET_MARKER not in str(exc_info.value)

    entries = await orch.plan_manager.read_ledger()
    assert _WALL_BUDGET_MARKER not in [e.op for e in entries]


@pytest.mark.asyncio
async def test_runner_cleanup_still_runs_on_budget_breach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worktree ``cleanup_all`` finally-block still runs on a budget
    breach — the new except clause must not swallow or bypass it."""
    orch, task = await _setup(tmp_path, impl_phase_wall_budget_s=5.0)
    monkeypatch.setattr(itr, "ImplTournament", _BudgetBreachTournament)

    cleanup_calls: list[bool] = []
    orig_cleanup_all = itr.WorktreeManager.cleanup_all

    async def _tracking_cleanup_all(self):  # type: ignore[no-untyped-def]
        cleanup_calls.append(True)
        return await orig_cleanup_all(self)

    monkeypatch.setattr(itr.WorktreeManager, "cleanup_all", _tracking_cleanup_all)

    with pytest.raises(TournamentError):
        await run_impl_tournament(orch, task, INITIAL_BUNDLE)

    assert cleanup_calls, "wt_mgr.cleanup_all() must still run on a budget breach"


# ── REAL-ENGINE tests: no monkeypatched ImplTournament ──────────────────
#
# Code-review finding: the five tests above monkeypatch ``ImplTournament``
# entirely, so the fake breach tournament fabricates the EXACT string the
# runner's guard checks for — that proves the WIRING but not that the REAL
# shared engine (``tournament.core.Tournament.run``, inherited unchanged by
# ``ImplTournament``) actually emits the impl-specific marker. Pre-fix, the
# engine's raise site was a HARDCODED ``"plan_phase_wall_budget_exceeded"``
# literal regardless of what ``TournamentConfig`` was passed — the guard
# above would never have matched in production, and this gap is exactly
# what these two tests close.


class _FakeClock:
    """Monotonic-shaped fake clock that advances a fixed step per read.

    Mirrors ``tests.test_tournament_wall_budget._FakeClock``. The Tournament
    reads the clock once at run-entry and once per between-pass check, so
    with ``step=10.0`` a ``wall_budget_s`` of 25 breaches after ~3 reads.
    """

    def __init__(self, step: float = 10.0, start: float = 1000.0) -> None:
        self._t = start
        self._step = step
        self.reads = 0

    def __call__(self) -> float:
        v = self._t
        self.reads += 1
        self._t += self._step
        return v


class _StubWorktrees:
    """Fake ``WorktreeManager``: ``create()`` returns a fixed directory. The
    stub ``CoderRunner`` below never inspects git state, so no real worktree
    is needed to drive ``ImplTournament.run_pass`` for real."""

    def __init__(self, base: Path) -> None:
        self._base = base

    async def create(self, nonce: str, base_ref: str = "HEAD") -> Path:
        return self._base


class _StubCoderRunner:
    """Fake ``CoderRunner``: returns a non-empty-diff ``ImplBundle`` without
    touching git or an adapter — keeps ``ImplTournament.run_pass`` executing
    for real while making zero real subprocess/Claude calls."""

    async def run(
        self, variant_label: str, direction: str, worktree: Path, task: ImplBundle
    ) -> ImplBundle:
        return ImplBundle(
            task_id=task.task_id,
            task_description=task.task_description,
            diff=f"diff --git a/x.py b/x.py\n+# variant {variant_label}\n",
            files_changed=["x.py"],
            tests_passed=1,
            tests_failed=0,
            tests_total=1,
            test_output_excerpt="1 passed",
            variant_label=variant_label,  # type: ignore[arg-type]
        )


def _impl_role_cb(role: str, system: str, user: str) -> str:
    """``StubLLMClient`` callback for the direct-engine test.

    Which candidate "wins" a given pass doesn't matter here: ``convergence_k``
    is set high enough (10) that no per-pass winner can trigger premature
    convergence before the fake-clock wall-budget breach fires (~pass 3).
    """
    if role == "critic_t":
        return "- nit"
    if role == "architect_b":
        return "fix the nit"
    if role == "synthesizer":
        return "merge both changes"
    return "RANKING: 1, 2, 3"


@pytest.mark.asyncio
async def test_real_engine_impl_marker_on_breach(tmp_path: Path) -> None:
    """A REAL ``ImplTournament`` (not monkeypatched) breaching its wall
    budget raises ``TournamentError`` carrying the IMPL marker, not the
    plan-phase one.

    Pre-fix this test FAILS: the shared engine's raise site was a hardcoded
    ``"plan_phase_wall_budget_exceeded"`` literal regardless of what
    ``TournamentConfig`` was passed, so an impl-tournament breach raised
    carrying the WRONG (plan-phase) marker.
    """
    clock = _FakeClock(step=10.0)
    cfg = itr.TournamentConfig(
        num_judges=1,
        convergence_k=10,  # never reached within the handful of passes below
        max_rounds=50,  # large: an early stop must be the budget, not the cap
        score_stability_window=None,
        score_stability_max_delta=None,
        winner_stability_window=None,
        wall_budget_s=25.0,
        wall_budget_marker=_WALL_BUDGET_MARKER,
        clock=clock,
    )
    tournament = ImplTournament(
        handler=ImplContentHandler(),
        client=StubLLMClient(fn=_impl_role_cb),
        cfg=cfg,
        artifact_dir=tmp_path / "tournaments" / "impl-wall-budget-direct",
        rng=random.Random(0xF00D),
        coder_runner=_StubCoderRunner(),
        worktree_manager=_StubWorktrees(tmp_path),
    )
    initial = ImplBundle(
        task_id="1.1",
        task_description="Add foo()",
        diff="diff --git a/x.py b/x.py\n+def foo(): pass\n",
        files_changed=["x.py"],
        tests_passed=1,
        tests_failed=0,
        tests_total=1,
        test_output_excerpt="1 passed",
    )

    with pytest.raises(TournamentError) as exc_info:
        await tournament.run(task_prompt="Add foo()", initial=initial)

    assert _WALL_BUDGET_MARKER in str(exc_info.value)
    assert "plan_phase_wall_budget_exceeded" not in str(exc_info.value)

    # Salvage incumbent still written — same engine code path the plan
    # tournament's core-level test pins.
    final_path = (
        tmp_path / "tournaments" / "impl-wall-budget-direct" / "final_output.md"
    )
    assert final_path.exists()


def _tournament_adapter() -> StubAdapter:
    """Build a StubAdapter with handlers for all tournament + coder roles.

    Mirrors ``tests.test_impl_tournament_runner._tournament_adapter``: the
    judge always ranks slot 1 first; combined with ``convergence_k=10`` set
    by the caller, no per-pass winner can trigger premature convergence
    before the fake-clock wall-budget breach fires.
    """

    def _handler(inv):  # type: ignore[no-untyped-def]
        role = inv.role
        if role == "developer":
            return AgentResult(
                success=True,
                text="implemented variant",
                diff="diff --git a/foo.py b/foo.py\n+def foo(): return 42",
                files_changed=[Path("foo.py")],
                duration_s=0.1,
            )
        if role == "test_engineer":
            return AgentResult(
                success=True,
                text="ran tests\nRESULTS: passed=3 failed=0 total=3",
                duration_s=0.1,
            )
        if role == "critic_t":
            return AgentResult(
                success=True, text="Critic: looks fine", duration_s=0.01
            )
        if role == "architect_b":
            return AgentResult(success=True, text="- minor fix", duration_s=0.01)
        if role == "synthesizer":
            return AgentResult(
                success=True, text="- synthesize both", duration_s=0.01
            )
        if role == "judge":
            return AgentResult(
                success=True,
                text="Good work.\n\nRANKING: 1, 2, 3",
                duration_s=0.01,
            )
        # judge_explorer / minimality_judge / any other specialist judge.
        return AgentResult(success=True, text=f"[stub:{role}]", duration_s=0.01)

    return StubAdapter(
        {
            "developer": _handler,
            "test_engineer": _handler,
            "critic_t": _handler,
            "architect_b": _handler,
            "synthesizer": _handler,
            "judge": _handler,
        }
    )


@pytest.mark.asyncio
async def test_runner_real_engine_breach_emits_ledger_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drives the REAL, un-monkeypatched ``ImplTournament`` to a wall-budget
    breach THROUGH the production ``run_impl_tournament()`` runner, and
    asserts the ``impl_phase_wall_budget_exceeded`` ledger op is actually
    written — not merely re-testing a fabricated mock string.

    ``run_impl_tournament`` builds its ``TournamentConfig`` with no
    test-only seam for injecting a fake ``clock`` (that seam is
    intentionally not exposed on the production signature). We monkeypatch
    the ``TournamentConfig`` NAME in the runner's own module namespace to a
    thin subclass that force-injects a fake clock while delegating every
    other field to the REAL, unmodified dataclass — the same "patch the
    name used at the call site" technique the wiring tests above already
    use for ``itr.ImplTournament``, applied one layer down.
    ``ImplTournament`` itself is NOT touched in this test.
    """
    _git_init(tmp_path)
    adapter = _tournament_adapter()
    orch = _make_orch(tmp_path, adapter, impl_phase_wall_budget_s=25.0)
    # Prevent premature convergence / runaway-detector early-stops so the
    # wall budget is unambiguously the ONLY early-stop cause (mirrors
    # test_tournament_wall_budget.py's core-level test).
    orch.cfg.tournaments.impl.convergence_k = 10
    orch.cfg.tournaments.impl.max_rounds = 50
    orch.cfg.tournaments.impl.score_stability_window = None
    orch.cfg.tournaments.impl.score_stability_max_delta = None
    orch.cfg.tournaments.impl.winner_stability_window = None
    await orch.plan_manager.init_plan(_mk_plan())
    task = await orch.plan_manager.get_task("1.1")
    assert task is not None

    clock = _FakeClock(step=10.0)

    class _FakeClockTournamentConfig(itr.TournamentConfig):  # type: ignore[misc]
        def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            kwargs.setdefault("clock", clock)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(itr, "TournamentConfig", _FakeClockTournamentConfig)

    with pytest.raises(TournamentError) as exc_info:
        await run_impl_tournament(orch, task, INITIAL_BUNDLE)
    assert _WALL_BUDGET_MARKER in str(exc_info.value)

    entries = await orch.plan_manager.read_ledger()
    ops = [e.op for e in entries]
    assert _WALL_BUDGET_MARKER in ops, (
        f"expected the loud ledger op from the REAL engine; got ops={ops}"
    )
    breach = next(e for e in entries if e.op == _WALL_BUDGET_MARKER)
    assert breach.payload["budget_s"] == 25.0
    assert breach.payload["task_id"] == "1.1"
    assert _WALL_BUDGET_MARKER in breach.payload["reason"]
