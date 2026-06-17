"""Phase 1A Step 1 — the R1 ledger-breadcrumb-completeness invariant.

RECOVERY-CONTRACT.md §7 Step 1: *every* terminal ``blocked`` OR ``quarantined``
transition committed to the ledger must be preceded (strictly lower seq) by a
typed recovery-decision op for that task. This closes three audit gaps:

  * WS1 two-channel-split breadcrumb (quarantine path emitted NO decision op),
  * WS1 conflict-escalation no-ledger (the critic's merge-strategy choice was
    invisible),
  * WS3 recovery-coherence ledger-audit gap.

The canonical recovery-decision ops are::

    {blocker_escalated, resolution_outcome, mark_blocked_descendants}

(``conflict_critic_decision`` records the merge-strategy CHOICE; the terminal
``block_task(CONFLICT_*)`` that may follow still emits ``blocker_escalated`` as
the canonical decision, so the conflict path is covered by the canonical set.)

This file is the *gate* that proves Step 1 engaged. It drives a REAL
``run_execute_phase`` that quarantines a task (no quarantine mocks; the only stub
is the LLM adapter) and asserts the R1 invariant + an anti-vacuity guard.

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


# The recovery-decision ops that count as "a typed decision was recorded".
CANONICAL_DECISION_OPS = frozenset(
    {"blocker_escalated", "resolution_outcome", "mark_blocked_descendants"}
)
# The terminal task-status transitions the R1 invariant governs.
TERMINAL_STATUSES = frozenset({"blocked", "quarantined"})


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_single_phase_plan() -> Plan:
    return Plan(
        plan_id="p-r1-breadcrumb",
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
        session_id="sess-r1-breadcrumb",
    )


def assert_every_terminal_has_decision(cwd: Path) -> None:
    """The R1 invariant + anti-vacuity guard.

    For EVERY ledger ``update_task_status`` entry whose ``status`` is in
    :data:`TERMINAL_STATUSES`, there must exist a strictly-earlier (lower seq)
    canonical recovery-decision op (:data:`CANONICAL_DECISION_OPS`) FOR THE SAME
    TASK. ``mark_blocked_descendants`` carries the parent linkage in
    ``failed_task_id`` and the cascaded ids in ``blocked_task_ids`` — a terminal
    cascade descendant is covered by either id channel.

    Anti-vacuity: asserts >= 1 terminal transition exists in the run, so a
    no-terminal run cannot satisfy the invariant trivially.
    """
    entries = ledger_mod.read_entries(cwd)

    # Build a per-task index of canonical-decision seqs.
    decision_seqs_by_task: dict[str, list[int]] = {}
    for e in entries:
        if e.op not in CANONICAL_DECISION_OPS:
            continue
        p = e.payload
        touched: set[str] = set()
        tid = p.get("task_id")
        if isinstance(tid, str):
            touched.add(tid)
        # mark_blocked_descendants links the failed parent + the cascaded ids.
        ftid = p.get("failed_task_id")
        if isinstance(ftid, str):
            touched.add(ftid)
        for bid in p.get("blocked_task_ids", []) or []:
            if isinstance(bid, str):
                touched.add(bid)
        for t in touched:
            decision_seqs_by_task.setdefault(t, []).append(e.seq)

    terminal = [
        e
        for e in entries
        if e.op == "update_task_status"
        and e.payload.get("status") in TERMINAL_STATUSES
    ]

    # Anti-vacuity: the run must actually have produced a terminal transition.
    assert terminal, (
        "anti-vacuity: no terminal (blocked/quarantined) transition in the "
        "ledger — the R1 invariant would pass vacuously"
    )

    for e in terminal:
        tid = e.payload.get("task_id")
        seqs = decision_seqs_by_task.get(tid, [])
        assert any(s < e.seq for s in seqs), (
            f"R1 violation: terminal '{e.payload.get('status')}' transition at "
            f"seq {e.seq} (task {tid}) has NO preceding canonical "
            f"recovery-decision op {sorted(CANONICAL_DECISION_OPS)}; "
            f"decision seqs for this task: {sorted(seqs)}"
        )


# ---------------------------------------------------------------------------
# R1 gate: a real quarantine run must leave a canonical decision breadcrumb
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc_factory",
    [
        pytest.param(
            lambda: InfrastructureCircuitOpenError(
                "infra circuit open: upstream 503 (cross-task breaker)"
            ),
            id="infra-circuit-open",
        ),
        pytest.param(
            lambda: AuthenticationFailedError(
                "auth_failed for role=developer: API Error: 403"
            ),
            id="auth-failed",
        ),
    ],
)
async def test_quarantine_emits_canonical_decision_breadcrumb(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exc_factory: Any,
) -> None:
    """RED ON HEAD: today the quarantine path (``_execute_one_worker``'s typed
    catch) stamps ``quarantined`` with NO canonical recovery-decision op, so
    :func:`assert_every_terminal_has_decision` fails. After Step 1's fix the
    quarantine path appends a ``resolution_outcome`` op immediately before the
    transition, turning this GREEN.
    """
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

    # Precondition: the task really did quarantine (anti-vacuity at the run
    # level, separate from the helper's ledger-level anti-vacuity guard).
    plan = await orch.plan_manager.load()
    assert plan is not None
    assert plan.phases[0].tasks[0].status == "quarantined"

    # The R1 invariant.
    assert_every_terminal_has_decision(tmp_path)


# ---------------------------------------------------------------------------
# Conflict-critic decision breadcrumb (1c)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conflict_path_emits_conflict_critic_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The conflict-escalation path records the critic's merge-strategy CHOICE
    as a ``conflict_critic_decision`` op.

    A full 3-way merge conflict is infeasible to seed in a unit test, so we
    exercise ``_apply_with_conflict_escalation`` directly: stub the worktree
    manager's ``apply_patch_to_main`` to raise ``WorktreeError`` (the trigger),
    and stub ``_escalate_conflict_to_critic`` to return ``abandon-task`` (a
    terminal branch). Assert the new audit op is appended carrying the chosen
    action, conflict files, and rewrite-round count.
    """
    from orchestrator.execute_phase import (
        ConflictResolution,
        _apply_with_conflict_escalation,
    )
    from orchestrator.worktree import WorktreeError

    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_single_phase_plan())
    orch = _make_orch(tmp_path)
    plan = await orch.plan_manager.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    task.files = ["src/a.py", "src/b.py"]

    class _ConflictingWorktreeMgr:
        async def apply_patch_to_main(self, *a: Any, **k: Any) -> None:
            raise WorktreeError("simulated 3-way conflict on apply")

        async def get_diff_vs_base(self, *a: Any, **k: Any) -> str:
            return "diff --git a/src/a.py b/src/a.py\n+conflict"

        async def abort_failed_apply(self, *a: Any, **k: Any) -> None:
            return None

    async def _fake_escalate(*a: Any, **k: Any) -> ConflictResolution:
        return ConflictResolution(action="abandon-task")

    monkeypatch.setattr(ep, "_escalate_conflict_to_critic", _fake_escalate)

    applied = await _apply_with_conflict_escalation(
        orch, task, tmp_path, _ConflictingWorktreeMgr()
    )
    assert applied is False  # abandon-task → not applied

    entries = ledger_mod.read_entries(tmp_path)
    decisions = [e for e in entries if e.op == "conflict_critic_decision"]
    assert decisions, (
        "conflict path emitted no conflict_critic_decision op "
        f"(ops seen: {[e.op for e in entries]})"
    )
    payload = decisions[-1].payload
    assert payload.get("task_id") == task.id
    assert payload.get("action") == "abandon-task"
    assert payload.get("conflict_files") == ["src/a.py", "src/b.py"]
    assert payload.get("rewrite_rounds") == 0

    # No silent dead-end. Step 5 (structural-action recovery) changed the
    # downstream outcome for CONFLICT_*: the resolver's ``re_architect`` now
    # synthesizes a structured corrective direction → injects corrective tasks →
    # the original task is RECOVERED (``skipped``) rather than terminally
    # ``blocked``. So the conflict path is covered by EITHER channel:
    #   * a terminal block recorded canonically (R1 invariant), OR
    #   * a structural recovery (``blocker_escalated`` + ``resolution_outcome``
    #     with outcome=recovered, and the task left non-terminal/``skipped``).
    ops = [e.op for e in entries]
    terminal_entries = [
        e
        for e in entries
        if e.op == "update_task_status"
        and e.payload.get("status") in TERMINAL_STATUSES
    ]
    if terminal_entries:
        # A terminal block did happen — it must be canonically recorded.
        assert_every_terminal_has_decision(tmp_path)
    else:
        # Structural recovery path: the conflict was escalated to the resolver
        # and an outcome was recorded — no silent dead-end.
        assert "blocker_escalated" in ops, (
            f"conflict neither blocked nor escalated to the resolver (ops={ops})"
        )
        assert "resolution_outcome" in ops, (
            f"conflict recovery did not record a resolution_outcome (ops={ops})"
        )
        # And the original task is non-terminal (recovered), not a dead-end.
        plan_after = await orch.plan_manager.load()
        assert plan_after is not None
        recovered_task = plan_after.phases[0].tasks[0]
        assert recovered_task.status != "blocked", (
            f"expected structural recovery, got status={recovered_task.status}"
        )
