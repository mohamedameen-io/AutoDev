"""Phase 1A Step 7 — route the dominant quarantine/infra path through the
resolver (gate R6).

RECOVERY-CONTRACT.md §7 Step 7: the dominant quarantine path
(``_execute_one_worker``'s ``except (AuthenticationFailedError,
InfrastructureCircuitOpenError)`` block) historically stamped
``update_task_status(..., "quarantined")`` with NO resolver escalation — it
bypassed the resolver entirely (the architect-consult infra path DID route
through ``block_task(INFRA_CIRCUIT_OPEN)``, so the two infra paths diverged).
Step 1 added a ``resolution_outcome`` breadcrumb here; Step 7 adds the resolver
CONSULTATION (``_maybe_resolve_blocker``) so a ``blocker_escalated`` op precedes
the quarantine.

For ``INFRA_CIRCUIT_OPEN`` the resolver's deterministic action is
``fall_through`` (``blocker_resolver.py:165``) → ``_apply_resolution`` returns
None → the task is NOT recovered → it falls through to the EXISTING quarantine
stamp unchanged. So the quarantine semantics are preserved (resumable /
non-terminal — NOT ``blocked``); the ONLY observable change is the
``blocker_escalated`` escalation op now lands BEFORE the ``quarantined``
transition.

Gate R6: ``InfrastructureCircuitOpenError`` produces a ``blocker_escalated`` op
with seq STRICTLY LESS THAN the first ``update_task_status(status="quarantined")``
transition for the task.

Broken-control (anti-vacuity): with the resolver OFF
(``AUTODEV_RESOLVER_DISABLED=1``), ``_maybe_resolve_blocker`` is a no-op → NO
``blocker_escalated`` precedes the quarantine → the R6 assertion goes RED. We
demonstrate that explicitly (and assert the task still quarantines, so the
control is not vacuous).

Marked ``resolver_enabled`` so the conftest autouse fixture unsets
``AUTODEV_RESOLVER_DISABLED`` (the suite scopes the resolver OFF by default).
"""

from __future__ import annotations

import datetime as _dt
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
from tournament.errors import (
    AuthenticationFailedError,
    InfrastructureCircuitOpenError,
)

from stub_adapter import StubAdapter, ok

pytestmark = pytest.mark.resolver_enabled


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_single_phase_plan() -> Plan:
    return Plan(
        plan_id="p-r6-infra-route",
        spec_hash="0123456789abcdef",
        phases=[
            Phase(
                id="1",
                title="Investigate",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="t1",
                        description="d1",
                        complexity="medium",
                    ),
                ],
                acceptance=[
                    AcceptanceCriterion(id="ph-ac-1", description="all good")
                ],
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
        complexity="medium",
    )


def _make_orch(cwd: Path) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.phase_review.enabled = True
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
        session_id="sess-r6-infra-route",
    )


def _first_quarantine_seq(cwd: Path, task_id: str) -> int:
    """Seq of the FIRST ``update_task_status(status="quarantined")`` transition
    for ``task_id`` in the ledger, or -1 if none exists."""
    for e in ledger_mod.read_entries(cwd):
        if (
            e.op == "update_task_status"
            and e.payload.get("status") == "quarantined"
            and e.payload.get("task_id") == task_id
        ):
            return e.seq
    return -1


def _escalation_seqs(cwd: Path, task_id: str) -> list[int]:
    """All ``blocker_escalated`` seqs for ``task_id`` (the resolver consult's
    escalation op)."""
    return [
        e.seq
        for e in ledger_mod.read_entries(cwd)
        if e.op == "blocker_escalated" and e.payload.get("task_id") == task_id
    ]


async def _drive_quarantine_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exc_factory: Any,
) -> Orchestrator:
    """Seed ``_execute_one`` to raise the given infra/auth exception, run the
    phase (tolerating the propagated typed exception), and return the orch."""
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_single_phase_plan())
    orch = _make_orch(tmp_path)

    async def _raise_quarantine(
        orch_arg: Any, task: Task, worktree_mgr: Any = None
    ) -> Task:
        await orch_arg.plan_manager.update_task_status(task.id, "in_progress")
        raise exc_factory()

    monkeypatch.setattr(ep, "_execute_one", _raise_quarantine)

    with pytest.raises(
        (AuthenticationFailedError, InfrastructureCircuitOpenError)
    ):
        await ep.run_execute_phase(orch)
    return orch


# ---------------------------------------------------------------------------
# R6 gate: escalation precedes quarantine on the dominant infra path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r6_infra_circuit_escalation_precedes_quarantine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED ON HEAD (pre-Step-7): the dominant quarantine path stamped
    ``quarantined`` with NO ``blocker_escalated`` op, so this ordering assertion
    failed. After Step 7's resolver consult, a ``blocker_escalated`` lands
    BEFORE the quarantine transition → GREEN.
    """
    orch = await _drive_quarantine_run(
        tmp_path,
        monkeypatch,
        lambda: InfrastructureCircuitOpenError(
            "infra circuit open: upstream 503 (cross-task breaker)"
        ),
    )

    # Precondition: the task really did quarantine — and is NOT terminal.
    plan = await orch.plan_manager.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    assert task.status == "quarantined", (
        f"expected quarantined, got {task.status!r}"
    )
    # The resolver's fall_through must NOT have turned the task terminal: a
    # quarantined task stays resumable; ``block_task`` ('blocked') is the wrong
    # channel here and must NOT be reached.
    assert task.status != "blocked"

    quarantine_seq = _first_quarantine_seq(tmp_path, task.id)
    assert quarantine_seq > 0, (
        "anti-vacuity: no quarantined transition recorded in the ledger"
    )

    escalation_seqs = _escalation_seqs(tmp_path, task.id)
    assert escalation_seqs, (
        "R6 violation: the infra quarantine path emitted NO blocker_escalated "
        "op (the resolver was not consulted before the quarantine)"
    )
    assert any(s < quarantine_seq for s in escalation_seqs), (
        f"R6 violation: no blocker_escalated op (seqs={sorted(escalation_seqs)}) "
        f"precedes the first quarantined transition (seq={quarantine_seq})"
    )


@pytest.mark.asyncio
async def test_r6_auth_failed_escalation_precedes_quarantine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same R6 invariant for the single-shot auth path. The Step-7 consult uses
    ``INFRA_CIRCUIT_OPEN`` as the failure_class for BOTH auth and circuit-open
    (the existing fall_through-quarantine class fits both), so an
    ``AuthenticationFailedError`` also routes through the resolver →
    ``blocker_escalated`` precedes its quarantine.
    """
    orch = await _drive_quarantine_run(
        tmp_path,
        monkeypatch,
        lambda: AuthenticationFailedError(
            "auth_failed for role=developer: API Error: 403"
        ),
    )

    plan = await orch.plan_manager.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    assert task.status == "quarantined", (
        f"expected quarantined, got {task.status!r}"
    )
    assert task.status != "blocked"

    quarantine_seq = _first_quarantine_seq(tmp_path, task.id)
    assert quarantine_seq > 0
    escalation_seqs = _escalation_seqs(tmp_path, task.id)
    assert escalation_seqs, (
        "R6 violation (auth path): no blocker_escalated op emitted"
    )
    assert any(s < quarantine_seq for s in escalation_seqs), (
        f"R6 violation (auth path): no blocker_escalated "
        f"(seqs={sorted(escalation_seqs)}) precedes quarantine "
        f"(seq={quarantine_seq})"
    )


# ---------------------------------------------------------------------------
# Broken-control / anti-vacuity: resolver OFF → R6 goes RED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r6_broken_control_resolver_off_no_escalation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anti-vacuity broken-control. With the resolver FORCE-disabled
    (``AUTODEV_RESOLVER_DISABLED=1``), ``_maybe_resolve_blocker`` is a no-op →
    NO ``blocker_escalated`` is emitted → the R6 ordering assertion would be
    RED. We assert the NEGATIVE (no escalation precedes quarantine) AND that the
    task still quarantines (so the control is not vacuous — the path ran, it
    just lacked the escalation).

    This proves the R6 green result above is caused by the Step-7 consult and
    not by some unrelated op happening to satisfy the ordering.
    """
    # The conftest autouse fixture would unset this for resolver_enabled tests;
    # re-set it AFTER fixture setup to force the resolver OFF for this case.
    monkeypatch.setenv("AUTODEV_RESOLVER_DISABLED", "1")

    orch = await _drive_quarantine_run(
        tmp_path,
        monkeypatch,
        lambda: InfrastructureCircuitOpenError(
            "infra circuit open: upstream 503 (cross-task breaker)"
        ),
    )

    plan = await orch.plan_manager.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    # Not vacuous: the quarantine path still ran end-to-end.
    assert task.status == "quarantined"

    quarantine_seq = _first_quarantine_seq(tmp_path, task.id)
    assert quarantine_seq > 0

    escalation_seqs = _escalation_seqs(tmp_path, task.id)
    # The broken-control: no escalation precedes the quarantine when the
    # resolver is off, so the positive R6 assertion would be RED here.
    assert not any(s < quarantine_seq for s in escalation_seqs), (
        "broken-control failed: a blocker_escalated preceded the quarantine "
        "even with the resolver disabled — the R6 gate is vacuous"
    )


@pytest.mark.asyncio
async def test_r6_broken_control_auth_resolver_off_no_escalation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same broken-control for the auth path."""
    monkeypatch.setenv("AUTODEV_RESOLVER_DISABLED", "1")

    orch = await _drive_quarantine_run(
        tmp_path,
        monkeypatch,
        lambda: AuthenticationFailedError(
            "auth_failed for role=developer: API Error: 403"
        ),
    )

    plan = await orch.plan_manager.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    assert task.status == "quarantined"

    quarantine_seq = _first_quarantine_seq(tmp_path, task.id)
    assert quarantine_seq > 0
    escalation_seqs = _escalation_seqs(tmp_path, task.id)
    assert not any(s < quarantine_seq for s in escalation_seqs)
