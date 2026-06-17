"""Step 4 (RECOVERY-CONTRACT §7; gate R3) — THE KEYSTONE gate.

Step 4 moves the resolver chokepoint (``_maybe_resolve_blocker``) BEFORE the
legacy escalation ladder (``next_step``) inside ``_try_retry_or_escalate``. Before
Step 4 the resolver was SHADOWED: ``next_step`` ran the REFINE/PIVOT/
ARCHITECT_CONSULT ladder (which returns WITHOUT ``block_task``) and the resolver
was only reached at the two terminal rungs — so it could re-enable a task but the
caller's loop had nothing wired to re-dispatch it, and the phase DAG loop WEDGED
(``PhaseStuckError``).

This module proves the keystone with a REAL run (``run_execute_phase`` over a
real ``Orchestrator`` + ``PlanManager`` + git repo) and an always-failing
developer (``worker_exception``). The gate has four legs:

  1. NO WEDGE: ``run_execute_phase`` completes WITHOUT raising ``PhaseStuckError``
     and the task ends terminal (``blocked`` is the correct outcome — a developer
     that never produces a usable diff cannot complete).
  2. RESOLVER REACHED BEFORE THE LADDER: the first ``blocker_escalated`` (resolver
     breadcrumb) precedes the first ``recovery_action_chosen`` (the legacy-ladder
     audit op) — proof the resolver is no longer shadowed by ``next_step``.
  3. BOUNDED TERMINATION: re-dispatching the still-failing developer re-consults
     the resolver, but the per-(task, failure_class) cycle cap
     (``_maybe_resolve_blocker``'s ``_resolver_cycle_counts`` backstop, bounded by
     ``cfg.resolver.max_cycles_per_blocker``) caps the resolver invocations and
     then the chokepoint declines → legacy ladder → terminal block. The test
     ASSERTS a bounded ``blocker_escalated`` count and that the run reaches a
     terminal (it cannot hang).
  4. BROKEN-CONTROL: with the resolver disabled (forcing it to decline at the
     chokepoint), the keystone is reverted in effect — the legacy terminal block
     still runs but the resolver no longer fires from the in-loop chokepoint, and
     (importantly) the no-shadow ordering assertion (leg 2) goes RED. We assert
     the broken-control directly so the gate is non-vacuous.

A separate CONFLICT test is carried forward as strict-xfail for Step 5: a
``CONFLICT_3WAY_FAILED`` reaches the resolver (Step 4 wiring) but its deterministic
action ``re_architect`` is INERT — it emits a prose rationale that
``parse_corrective_direction`` turns into 0 corrective tasks, so the resolver
declines and the task blocks. Step 5 makes the structural action actually emit
corrective tasks; until then ``status != "blocked"`` is RED.

Double-consultation note: the terminal retry-exhaustion rung still calls
``block_task`` (which re-consults the resolver). That second consult shares the
SAME per-blocker cycle key as the in-loop chokepoint, so the in-memory cap bounds
the TOTAL resolver invocations across both sites — no redundant unbounded
re-resolution. We deliberately keep it simple (no pass-through skip signal); the
cap already makes the terminal re-consult bounded and cheap.
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
from orchestrator import failure_classes as _fcls
from state import ledger as ledger_mod
from state.plan_manager import PlanManager
from state.schemas import AcceptanceCriterion, Phase, Plan, Task

from stub_adapter import StubAdapter, fail, ok

pytestmark = pytest.mark.resolver_enabled

_TERMINAL_STATUSES = frozenset({"complete", "blocked", "skipped"})


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan() -> Plan:
    return Plan(
        plan_id="p-step4-keystone",
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
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=str(repo), check=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True)
    (repo / "math_utils.py").write_text("def add(a, b):\n    return a + b\n")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(repo), check=True)


def _make_cfg() -> Any:
    cfg = default_config()
    cfg.tournaments.phase_review.enabled = False
    cfg.tournaments.impl.enabled = False
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.auto_disable_for_models = []
    cfg.qa_retry_min_interval_s = 0.0
    cfg.qa_retry_limit = 1
    return cfg


async def _build_orch(repo: Path, *, session: str) -> Orchestrator:
    cfg = _make_cfg()
    registry = build_registry(cfg)
    adapter = StubAdapter(
        {"explorer": ok("ok"), "developer": fail("developer always fails")}
    )
    pm = PlanManager(repo, session_id=f"{session}-init")
    await pm.init_plan(_mk_plan())
    return Orchestrator(
        cwd=repo,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id=f"{session}-exec",
    )


def _ops(cwd: Path) -> list[str]:
    return [e.op for e in ledger_mod.read_entries(cwd)]


# ---------------------------------------------------------------------------
# The keystone gate: resolver reached BEFORE the ladder, no wedge, bounded.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolver_intercepts_before_ladder_no_wedge_bounded(
    tmp_path: Path,
) -> None:
    """Step-4 gate: a real run with an always-failing developer must complete
    WITHOUT ``PhaseStuckError`` (no wedge), reach a terminal, fire the resolver
    BEFORE the legacy ladder, and terminate in a BOUNDED number of resolver
    cycles."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    orch = await _build_orch(repo, session="step4-keystone")

    # Leg 1 — NO WEDGE: this must NOT raise. If Step 4 is reverted, the resolver
    # re-enables the task with nothing wired to re-dispatch it and the phase DAG
    # loop raises PhaseStuckError. We assert completion directly.
    await ep.run_execute_phase(orch)  # would raise PhaseStuckError pre-Step-4

    plan = await orch.plan_manager.load()
    assert plan is not None
    statuses = [t.status for t in plan.phases[0].tasks]
    assert all(s in _TERMINAL_STATUSES for s in statuses), (
        f"task left non-terminal (wedge): {statuses}"
    )
    # The correct terminal for a developer that never produces a usable diff.
    assert statuses == ["blocked"], statuses

    ops = _ops(repo)

    # Leg 2 — RESOLVER REACHED, and BEFORE the legacy ladder. The resolver
    # breadcrumb (blocker_escalated) is the in-loop chokepoint; the legacy ladder
    # audit op is recovery_action_chosen. Pre-Step-4 the ladder ran first
    # (recovery_action_chosen precedes blocker_escalated); Step 4 inverts that.
    assert "blocker_escalated" in ops, (
        f"resolver was NOT reached from the real loop (ops={ops})"
    )
    assert "resolution_chosen" in ops, (
        f"resolver escalated but chose no resolution (ops={ops})"
    )
    first_resolver = ops.index("blocker_escalated")
    if "recovery_action_chosen" in ops:
        first_ladder = ops.index("recovery_action_chosen")
        assert first_resolver < first_ladder, (
            "resolver is STILL SHADOWED: the legacy ladder "
            f"(recovery_action_chosen @ {first_ladder}) ran before the resolver "
            f"chokepoint (blocker_escalated @ {first_resolver}). Step 4 must place "
            f"the resolver FIRST. ops={ops}"
        )

    # Leg 3 — BOUNDED TERMINATION. The run reached a terminal (asserted above,
    # i.e. it did NOT hang), and the resolver was consulted a BOUNDED number of
    # times: the per-blocker cycle cap (cfg.resolver.max_cycles_per_blocker)
    # plus at most the single terminal block_task re-consult. We assert the count
    # is >= 1 (it engaged) and <= cap + 1 (it did not loop unboundedly).
    cap = int(orch.cfg.resolver.max_cycles_per_blocker)
    escalations = ops.count("blocker_escalated")
    assert 1 <= escalations <= cap + 1, (
        f"resolver invocation count {escalations} not bounded by cap+1={cap + 1} "
        f"(would indicate an unbounded recovery loop). ops={ops}"
    )


@pytest.mark.asyncio
async def test_broken_control_disabled_resolver_reverts_no_shadow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Broken-control for the keystone: force the resolver to DECLINE at the
    chokepoint (``AUTODEV_RESOLVER_DISABLED=1``). With the in-loop chokepoint a
    no-op, the resolver no longer intercepts before the ladder — so the no-shadow
    ordering (leg 2 of the keystone gate) is no longer satisfiable: the run drives
    the LEGACY path (the ladder's recovery_action_chosen fires) and NO
    ``blocker_escalated`` precedes it. Proves the keystone gate is non-vacuous —
    its central assertion genuinely depends on the resolver intercepting first.

    Note: with a low retry limit the disabled-resolver legacy path reaches a clean
    terminal block (no wedge), exactly as the pre-resolver suite encodes — so the
    relevant broken-control signal here is the LOSS of the resolver-before-ladder
    ordering, not a wedge."""
    monkeypatch.setenv("AUTODEV_RESOLVER_DISABLED", "1")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    orch = await _build_orch(repo, session="step4-broken")

    # Legacy path with a shallow retry limit terminates cleanly (no wedge).
    await ep.run_execute_phase(orch)

    ops = _ops(repo)
    # The resolver chokepoint is a pure no-op when disabled: no resolver
    # breadcrumb is ever emitted, so the keystone's "resolver reached BEFORE the
    # ladder" assertion cannot hold.
    assert "blocker_escalated" not in ops, (
        "resolver fired despite AUTODEV_RESOLVER_DISABLED=1 — chokepoint not "
        f"honouring the kill-switch (ops={ops})"
    )
    # The legacy ladder still runs (recovery_action_chosen), and the task still
    # terminally blocks — proving the loop genuinely ran the legacy path.
    assert "recovery_action_chosen" in ops, (
        f"legacy ladder did not run on the disabled-resolver path (ops={ops})"
    )
    assert any(
        e.op == "update_task_status" and e.payload.get("status") == "blocked"
        for e in ledger_mod.read_entries(repo)
    ), "expected a terminal block even with the resolver disabled"


# ---------------------------------------------------------------------------
# Carry-forward for Step 5: the resolver REACHES a CONFLICT_3WAY_FAILED but its
# structural action (re_architect) is INERT, so it cannot recover → blocks.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Step 5: structural-action inertness — for CONFLICT_3WAY_FAILED the "
        "deterministic ladder picks re_architect, which emits a prose rationale; "
        "parse_corrective_direction turns prose into 0 corrective tasks → "
        "_resolver_corrective returns None → resolver declines → the task BLOCKS. "
        "Step 4 only moves the chokepoint EARLIER (the resolver now REACHES the "
        "conflict), but it cannot recover structurally until Step 5 makes "
        "re_architect emit real corrective tasks. Until then status == 'blocked'."
    ),
)
@pytest.mark.asyncio
async def test_conflict_3way_resolver_recovers_structurally(
    tmp_path: Path,
) -> None:
    """Step-5 carry-forward: a CONFLICT_3WAY_FAILED routed through the Step-4
    chokepoint should be actively recovered (``status != 'blocked'``). RED on
    Step-4 HEAD: the resolver REACHES the conflict but ``re_architect`` is inert
    (prose → 0 corrective tasks → decline → block). Proves the
    resolver-reaches-it-but-can't-recover-structurally gap that Step 5 closes."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    orch = await _build_orch(repo, session="step4-conflict")
    plan = await orch.plan_manager.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    task = await orch.plan_manager.update_task_status(task.id, "in_progress")

    # Drive the chokepoint with a CONFLICT_3WAY_FAILED (structural) class. Under
    # resolver_enabled the chokepoint REACHES the resolver, which picks
    # re_architect — inert prose → declines → falls through to the legacy ladder
    # and (with qa_retry_limit=1) terminally blocks.
    result = await ep._try_retry_or_escalate(
        orch,
        task,
        retry_limit=orch.cfg.qa_retry_limit,
        reason="three-way merge could not be resolved mechanically",
        failure_class=_fcls.CONFLICT_3WAY_FAILED,
    )

    # XPASS only when Step 5 makes re_architect emit corrective tasks (status
    # would become 'skipped'/re-enabled, not 'blocked').
    assert getattr(result, "status", None) != "blocked", (
        "resolver recovered the CONFLICT_3WAY_FAILED structurally (Step 5 landed)"
    )
