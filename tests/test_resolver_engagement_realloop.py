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
      resolver was REACHED from the real loop (not bypassed). **Green on HEAD.**
  (b) the resolution's ``failure_class`` is a REAL class, NOT ``"unknown"`` — the
      resolver should classify the failure, not treat the whole real-loop path
      as a novel/unseen failure. **Red on HEAD.**
  (c) the task actually RECOVERED (final status != ``"blocked"``) — the truest
      engagement signal. **Red on HEAD.**

Per the RECOVERY-CONTRACT, on current HEAD the resolver is SHADOWED by the
legacy escalation ladder: a failing developer with a low retry limit drives the
``next_step == "continue"`` legacy retry-exhaustion path
(``execute_phase.py``, the ``retry_exhausted`` block), which calls
``block_task(failure_class=_fcls.UNKNOWN, ...)``. UNKNOWN routes to the LLM
resolver; the stubbed/unparseable resolver response yields ``ask_human``,
``_apply_resolution`` falls through, and ``block_task`` commits ``blocked``. So
the resolver **fires but falls through to blocked** with ``failure_class ==
"unknown"`` — exactly the field failure mode the contract predicts.

Therefore (b) and (c) are RED on HEAD and live under
``@pytest.mark.xfail(strict=True)``. They are the carry-forward signal: when
Phase 1A fixes the resolver (real classification + real recovery on this path)
these tests XPASS, ``strict`` flips XPASS -> FAIL, and we are FORCED to delete
the marker. (a) is genuinely green on HEAD and stays a LIVE (non-xfail)
assertion.

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

_XFAIL_REASON = (
    "Phase 1A: WS1-resolver-shadowed-by-ladder + structural-actions-inert — "
    "resolver fires but falls through / class=UNKNOWN"
)


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
    await ep.run_execute_phase(orch)
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


@pytest.mark.xfail(strict=True, reason=_XFAIL_REASON)
@pytest.mark.asyncio
async def test_resolver_records_real_failure_class(tmp_path: Path) -> None:
    """R2 gate (b): the resolution breadcrumbs carry a REAL failure class, not
    the catch-all ``"unknown"``.

    RED on HEAD: the failing-developer path drives the legacy retry-exhaustion
    escalation, which blocks with ``failure_class=_fcls.UNKNOWN``, so every
    ``blocker_escalated`` / ``resolution_chosen`` breadcrumb on this path
    records ``"unknown"``. Phase 1A makes the resolver classify the real-loop
    failure (a developer that never produces a usable diff is a concrete
    failure class, not a novel/unseen one) → this XPASSes → strict-xfail FAILS →
    forces removal of the marker."""
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
# (c) — RED on HEAD: the task actually RECOVERS (final status != blocked).
# ---------------------------------------------------------------------------


@pytest.mark.xfail(strict=True, reason=_XFAIL_REASON)
@pytest.mark.asyncio
async def test_resolver_recovers_task_in_real_loop(tmp_path: Path) -> None:
    """R2 gate (c): the truest engagement signal — after the resolver fires from
    the real loop, the task is NOT left ``blocked``.

    RED on HEAD: the resolver fires but falls through (LLM resolver returns
    ``ask_human`` on the UNKNOWN class, ``_apply_resolution`` returns ``None``),
    so ``block_task`` commits ``blocked``. Phase 1A makes the resolver actively
    recover this path → final status != ``"blocked"`` → XPASS → strict-xfail
    FAILS → forces removal of the marker."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    orch, _cwd = await _drive_failing_developer(repo)

    status = await _final_task_status(orch)
    assert status != "blocked", (
        "resolver fired but fell through — task left blocked instead of "
        f"recovered (status={status!r})"
    )
