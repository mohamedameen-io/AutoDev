"""Phase 0 / B3 — the R2 engagement gate: the Universal Blocker Resolver must
ENGAGE the REAL execute loop, not just be callable in isolation.

``tests/test_resolver_engagement.py`` calls ``blocker_guard.block_task()``
*directly*; it never drives ``run_execute_phase``, so "the resolver is reachable
from the real loop" was UNPROVEN. This module closes that gap
(``WS3-engagement-gate-direct-block-task-bypass``) by building a REAL
``Orchestrator`` + ``PlanManager`` + ``StubAdapter`` whose ``developer`` role
FAILS every call, then awaiting ``run_execute_phase`` until the loop drives the
task to a *terminal* block site. We then audit the on-disk ledger.

The R2 gate has three assertions:

  (a) ``blocker_escalated`` AND ``resolution_chosen`` are in the ledger ops — the
      resolver was REACHED from the real loop (not bypassed). **Green since HEAD.**
  (b) the resolution's ``failure_class`` is a REAL class, NOT ``"unknown"`` — the
      resolver classifies the failure, not treating the whole real-loop path as a
      novel/unseen failure. **LIVE since Step 3.**
  (c) the resolver recovers WITHOUT WEDGING — the real loop reaches a clean
      terminal with no ``PhaseStuckError``. **RED until Step 4** (re-dispatch of a
      resolver-recovered task is not yet wired), carried as strict-xfail.

On HEAD the resolver was SHADOWED by the legacy escalation ladder: a failing
developer with a low retry limit drove the ``next_step == "continue"`` legacy
retry-exhaustion path, which called ``block_task(failure_class=_fcls.UNKNOWN)``.
UNKNOWN routed to the LLM resolver; the stubbed response yielded ``ask_human``,
``_apply_resolution`` fell through, and ``block_task`` committed ``blocked`` —
so the resolver fired but fell through to blocked with ``failure_class ==
"unknown"``.

Step 3 (RECOVERY-CONTRACT §7) added a REQUIRED ``failure_class`` to
``_try_retry_or_escalate`` and threaded the REAL terminal class through to
``block_task``: the failing-developer path now classifies as ``worker_exception``
→ the deterministic ladder's first rung is ``retry_with_changes`` → the resolver
ACTIVELY recovers (re-enables the task) instead of falling through. That flipped
(b) from strict-xfail to LIVE green. (c) does NOT flip yet: re-dispatch of a
resolver-recovered task is wired in Step 4, so until then the re-enabled task is
left ``in_progress`` with nothing to re-spawn and the phase DAG loop raises
``PhaseStuckError`` — i.e. the resolver recovers but the loop WEDGES. (c) asserts
the absence of that wedge and stays strict-xfail until Step 4 (when it XPASSes →
forces removal of the marker). For (a)/(b) the ledger-audit driver tolerates the
intermediate wedge so the breadcrumbs (written before the wedge) are readable.

Engagement-first / no-vacuous-gate guarantee: the LIVE (a) assertion drives the
resolver through the *real* loop, so a planted resolver-disable must turn it
red. With ``AUTODEV_RESOLVER_DISABLED=1`` the chokepoint is a zero-cost no-op
(no ``blocker_escalated`` / ``resolution_chosen`` ops are ever appended), so (a)
fails — proven by ``test_live_assertion_a_goes_red_when_resolver_disabled``
below. After B6 wires ``AUTODEV_RESOLVER_FORCE_DISABLED=1`` into the conftest as
the canonical planted-disable knob, that same teardown invariant holds through
the standard switch.
"""

from __future__ import annotations

import datetime as _dt
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator import execute_phase as ep
from state import ledger as ledger_mod
from state.plan_manager import PlanManager
from state.schemas import AcceptanceCriterion, Phase, Plan, Task

from stub_adapter import StubAdapter, fail, ok

pytestmark = pytest.mark.resolver_enabled


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan() -> Plan:
    return Plan(
        plan_id="p-resolver-realloop",
        spec_hash="0123456789abcdef",
        phases=[
            Phase(
                id="1",
                title="Setup",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="t1",
                        description="d1",
                        complexity="medium",
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
    """Initialise a real git repo with a single tracked module.

    A git-initialised repo makes ``run_execute_phase`` take its per-task
    worktree path (the production path), not the bare-dir fallback.
    """
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=str(repo), check=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True)
    (repo / "math_utils.py").write_text("def add(a, b):\n    return a + b\n")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(repo), check=True)


def _make_cfg() -> Any:
    """Deterministic config: tournaments off, retries fast + shallow so a
    failing developer hits a terminal block site quickly."""
    cfg = default_config()
    cfg.tournaments.phase_review.enabled = False
    cfg.tournaments.impl.enabled = False
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.auto_disable_for_models = []
    # No 30s backoff between retries — keep the test fast.
    cfg.qa_retry_min_interval_s = 0.0
    # One retry then escalate: minimises adapter calls to reach the terminal
    # block site without tripping the ladder's pivot/architect rungs.
    cfg.qa_retry_limit = 1
    return cfg


async def _drive_failing_developer(repo: Path) -> tuple[Orchestrator, Path]:
    """Build a REAL run with a developer that fails every call and drive
    ``run_execute_phase`` to its terminal block site. Returns the
    orchestrator and the repo path (the ledger root)."""
    cfg = _make_cfg()
    registry = build_registry(cfg)
    # The developer FAILS every call (success=False). disable_impl_tournament
    # via the cfg above keeps the path deterministic. ``explorer`` is stubbed
    # so any pre-developer probing succeeds.
    adapter = StubAdapter(
        {
            "explorer": ok("ok"),
            "developer": fail("developer always fails"),
        }
    )

    pm = PlanManager(repo, session_id="realloop-init")
    await pm.init_plan(_mk_plan())

    orch = Orchestrator(
        cwd=repo,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="realloop-exec",
    )
    # Step 3: the failing-developer path now threads a REAL class
    # (``worker_exception``), so the resolver ACTIVELY recovers it (the
    # deterministic ladder's first rung is ``retry_with_changes``) and re-enables
    # the task to ``in_progress``. Re-dispatch of a resolver-recovered task is
    # wired in Step 4/5; until then the phase DAG loop has no pending task to
    # re-spawn and raises ``PhaseStuckError``. All resolver breadcrumbs are
    # written to the on-disk ledger BEFORE that point, so the gate assertions
    # (which audit the ledger) hold; we tolerate the Step-4/5-pending wedge here.
    from errors import PhaseStuckError

    try:
        await ep.run_execute_phase(orch)
    except PhaseStuckError:
        pass
    return orch, repo


def _ops(cwd: Path) -> list[str]:
    return [e.op for e in ledger_mod.read_entries(cwd)]


def _resolution_failure_classes(cwd: Path) -> list[str | None]:
    """The ``failure_class`` recorded on every resolver breadcrumb
    (``blocker_escalated`` + ``resolution_chosen``)."""
    return [
        e.payload.get("failure_class")
        for e in ledger_mod.read_entries(cwd)
        if e.op in ("blocker_escalated", "resolution_chosen")
    ]


async def _final_task_status(orch: Orchestrator) -> str | None:
    plan = await orch.plan_manager.load()
    assert plan is not None
    return plan.phases[0].tasks[0].status


# ---------------------------------------------------------------------------
# (a) — LIVE, green on HEAD: the resolver is REACHED from the real loop.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolver_reached_from_real_loop(tmp_path: Path) -> None:
    """R2 gate (a): driving ``run_execute_phase`` with a failing developer to a
    terminal block site leaves BOTH ``blocker_escalated`` and
    ``resolution_chosen`` in the on-disk ledger — proof the resolver fired from
    the REAL loop (not the ``block_task``-direct path the legacy tests use).

    Green on HEAD. This is the non-vacuous engagement anchor: a planted
    resolver-disable turns it red (see
    ``test_live_assertion_a_goes_red_when_resolver_disabled``)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    _orch, cwd = await _drive_failing_developer(repo)

    ops = _ops(cwd)
    assert "blocker_escalated" in ops, (
        "resolver was NOT reached from the real loop — no blocker_escalated op "
        f"(ops={ops})"
    )
    assert "resolution_chosen" in ops, (
        "resolver escalated but chose no resolution — no resolution_chosen op "
        f"(ops={ops})"
    )


@pytest.mark.asyncio
async def test_live_assertion_a_goes_red_when_resolver_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-vacuous guard for gate (a): with the resolver chokepoint disabled,
    the real loop emits NO resolver breadcrumbs, so gate (a) fails.

    This proves (a) genuinely depends on the resolver engaging — it cannot pass
    on the empty/found-nothing case. The kill-switch honoured here today is
    ``AUTODEV_RESOLVER_DISABLED``; after B6 the conftest planted-disable knob
    ``AUTODEV_RESOLVER_FORCE_DISABLED=1`` flows through the same chokepoint gate
    and this invariant holds unchanged."""
    monkeypatch.setenv("AUTODEV_RESOLVER_DISABLED", "1")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    _orch, cwd = await _drive_failing_developer(repo)

    ops = _ops(cwd)
    # The chokepoint is a zero-cost no-op when disabled: no resolver ops appear,
    # even though the developer failed and the task still terminally blocked.
    assert "blocker_escalated" not in ops, (
        "resolver fired despite AUTODEV_RESOLVER_DISABLED=1 — the kill-switch "
        f"is not honoured at the chokepoint (ops={ops})"
    )
    # Sanity: the task still reached a terminal block, so the loop genuinely ran.
    assert any(
        e.op == "update_task_status" and e.payload.get("status") == "blocked"
        for e in ledger_mod.read_entries(cwd)
    ), "expected a terminal block even with the resolver disabled"


# ---------------------------------------------------------------------------
# (b) — RED on HEAD: the resolver records a REAL failure_class, not "unknown".
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolver_records_real_failure_class(tmp_path: Path) -> None:
    """R2 gate (b): the resolution breadcrumbs carry a REAL failure class, not
    the catch-all ``"unknown"``.

    LIVE since Step 3 (RECOVERY-CONTRACT §7): ``_try_retry_or_escalate`` now
    threads the REAL terminal class through to ``block_task``, so the
    failing-developer path classifies as ``worker_exception`` (a developer that
    never produces a usable diff is a concrete failure class, not a novel/unseen
    one) instead of ``"unknown"``. Was strict-xfail on HEAD; the Step-3 change
    made it XPASS, which (per the carry-forward contract) forced removal of the
    marker — this is now a live green assertion."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    _orch, cwd = await _drive_failing_developer(repo)

    classes = _resolution_failure_classes(cwd)
    assert classes, "no resolver breadcrumbs to inspect — gate (a) precondition"
    assert all(c != "unknown" for c in classes), (
        "resolver recorded failure_class='unknown' on the real-loop path — it "
        f"treated a concrete failure as a novel/unseen one (classes={classes})"
    )


# ---------------------------------------------------------------------------
# (c) — RED until Step 4: the resolver recovers WITHOUT WEDGING the loop.
# ---------------------------------------------------------------------------

_TERMINAL_STATUSES = frozenset({"complete", "blocked", "skipped"})


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Step 4 (move resolver before next_step): the resolver now re-enables a "
        "worker_exception task to in_progress, but re-dispatch of a "
        "resolver-recovered task is not wired until Step 4 — so the phase DAG "
        "loop has no pending task to re-spawn and raises PhaseStuckError (the "
        "loop WEDGES). Step 4 wires re-dispatch → the loop drives the re-enabled "
        "task to a clean terminal with no wedge → XPASS → remove this marker."
    ),
)
@pytest.mark.asyncio
async def test_resolver_recovers_without_wedging(tmp_path: Path) -> None:
    """R2 gate (c): the resolver must recover the task WITHOUT wedging the loop.

    The always-failing developer SHOULD eventually reach a clean terminal (it
    never produces a usable diff, so a clean ``blocked`` is the correct outcome).
    The engagement signal across Steps 3→4 is that the resolver's re-enable does
    not strand the task: ``run_execute_phase`` must NOT raise ``PhaseStuckError``
    and every task must end terminal.

    RED until Step 4: Step 3 makes the resolver re-enable the task to
    ``in_progress`` (real ``worker_exception`` class → ``retry_with_changes``),
    but with no re-dispatch wired the phase DAG loop wedges (``PhaseStuckError``).
    Step 4 moves the resolver before ``next_step`` so the loop re-dispatches the
    re-enabled task and drives it to a clean terminal — flipping this green.

    (A separate Step-5 e2e test will prove a *recoverable* failure actually
    completes once resolver guidance is injected into ``last_issues``.)"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    cfg = _make_cfg()
    registry = build_registry(cfg)
    adapter = StubAdapter(
        {"explorer": ok("ok"), "developer": fail("developer always fails")}
    )
    pm = PlanManager(repo, session_id="realloop-c-init")
    await pm.init_plan(_mk_plan())
    orch = Orchestrator(
        cwd=repo, cfg=cfg, adapter=adapter, registry=registry,
        session_id="realloop-c-exec",
    )

    # No PhaseStuckError tolerance here — wedging IS the failure (c) detects.
    await ep.run_execute_phase(orch)  # raises PhaseStuckError on Step 3 (xfail)

    plan = await orch.plan_manager.load()
    assert plan is not None
    statuses = [t.status for t in plan.phases[0].tasks]
    assert all(s in _TERMINAL_STATUSES for s in statuses), (
        f"resolver-recovered task left non-terminal (wedge): {statuses}"
    )
