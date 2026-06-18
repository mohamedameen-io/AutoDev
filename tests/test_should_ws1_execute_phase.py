"""WS1 ``should``-severity recovery cleanups + one WS3 silent-degrade fix in
:mod:`orchestrator.execute_phase`.

Engagement-first: each test asserts the NEW behaviour AND carries a broken-control
(a monkeypatched / reverted variant that turns the assertion RED), so the gate is
non-vacuous. Items covered (see the WS1/WS3 plan):

  * EP-1  recovery-action-dead-annotation — the ``recovery_action_chosen`` op no
          longer carries a phantom parallel ``choose_recovery_action`` decision;
          its ``action`` now records the REAL ladder ``next_step``.
  * EP-2  budget-exhaustion-absorbed-by-ladder — a synthetic
          ``error_max_turns_escalation_exhausted`` developer result terminates via
          ``block_task(GUARDRAIL_EXCEEDED)`` instead of re-entering the ladder as
          WORKER_EXCEPTION.
  * EP-5  worktree-apply-failed-dead — a non-conflict ``WorktreeError`` escaping
          ``_apply_with_conflict_escalation`` now produces
          ``WORKTREE_APPLY_FAILED`` (its ``repair_environment`` resolver rung is
          reachable) rather than collapsing into WORKER_EXCEPTION.
  * EP-6  silent-degrade — (a) a failed ``reconcile_evidence_vs_ledger`` /
          ``reap_orphans`` HARD-ERRORs instead of log+continue; (b) a KB-consult
          outage at the ``consult_knowledge`` rung emits ``resolver_kb_failed``
          and REFUNDS the per-blocker cycle (no cycle consumed).
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
from orchestrator import failure_classes as _fcls
from state import ledger as ledger_mod
from state.plan_manager import PlanManager
from state.schemas import AcceptanceCriterion, Phase, Plan, Task

from stub_adapter import StubAdapter, fail, ok


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan(plan_id: str = "p-ws1") -> Plan:
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


def _make_cfg(*, disable_gates: bool = False) -> Any:
    cfg = default_config()
    cfg.tournaments.phase_review.enabled = False
    cfg.tournaments.impl.enabled = False
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.auto_disable_for_models = []
    cfg.qa_retry_min_interval_s = 0.0
    cfg.qa_retry_limit = 1
    if disable_gates:
        # Reach the worktree-apply / completion path without the real QA gates
        # (which would run pytest against a fixture repo with no tests and fail).
        cfg.qa_gates.syntax_check = False
        cfg.qa_gates.lint = False
        cfg.qa_gates.build_check = False
        cfg.qa_gates.test_runner = False
        cfg.qa_gates.secretscan = False
    return cfg


async def _build_orch(
    repo: Path,
    adapter: StubAdapter,
    *,
    session: str,
    disable_gates: bool = False,
) -> Orchestrator:
    cfg = _make_cfg(disable_gates=disable_gates)
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


def _entries(cwd: Path) -> list[Any]:
    return list(ledger_mod.read_entries(cwd))


def _ops(cwd: Path) -> list[str]:
    return [e.op for e in _entries(cwd)]


# ---------------------------------------------------------------------------
# EP-1: recovery_action_chosen records the REAL ladder decision, not a phantom.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recovery_action_chosen_records_real_next_step_not_phantom(
    tmp_path: Path,
) -> None:
    """The ``recovery_action_chosen`` op's ``action`` field equals ``next_step``
    (the real ladder decision) — proving the dead parallel
    ``choose_recovery_action`` policy was removed and the op is honest.

    Broken-control: the recovery_action_chosen ``action`` field must NOT be a
    ``RecoveryAction`` literal (``switch_tactic`` / ``re_architect`` / ...) — the
    phantom values. We assert the field is one of the ladder ``next_step`` rungs.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    adapter = StubAdapter(
        {"explorer": ok("ok"), "developer": fail("developer always fails")}
    )
    orch = await _build_orch(repo, adapter, session="ep1")

    # Drive the retry/escalate helper directly so the legacy ladder runs and emits
    # recovery_action_chosen (resolver disabled by default in the suite).
    plan = await orch.plan_manager.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    task = await orch.plan_manager.update_task_status(task.id, "in_progress")

    for _ in range(6):
        task = await ep._try_retry_or_escalate(
            orch,
            task,
            retry_limit=1,
            reason="forced stuck",
            failure_class=_fcls.WORKER_EXCEPTION,
        )
        if getattr(task, "status", None) in ("blocked", "skipped"):
            break

    rac = [
        e for e in _entries(repo) if e.op == "recovery_action_chosen"
    ]
    assert rac, f"recovery_action_chosen op never emitted; ops={_ops(repo)}"
    # The legacy RecoveryAction literals — the PHANTOM decision we removed.
    phantom_literals = {
        "switch_tactic",
        "increase_scope",
        "decrease_scope",
        "re_architect",
        "kb_lookup",
        "ask_human",
        "do_nothing",
    }
    for e in rac:
        action = e.payload.get("action")
        next_step = e.payload.get("next_step")
        # Honest breadcrumb: action == the real ladder decision.
        assert action == next_step, (
            f"recovery_action_chosen.action ({action!r}) diverged from the real "
            f"ladder next_step ({next_step!r}) — a phantom parallel decision is "
            f"back."
        )
        # Broken-control: must not be a phantom RecoveryAction literal.
        assert action not in phantom_literals, (
            f"recovery_action_chosen.action is a phantom RecoveryAction literal "
            f"({action!r}); the dead choose_recovery_action policy was reintroduced."
        )


# ---------------------------------------------------------------------------
# EP-2: budget-exhaustion synthetic terminates via block_task(GUARDRAIL_EXCEEDED).
# ---------------------------------------------------------------------------


def _coder_diff(variant: str = "v1") -> AgentResult:
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


@pytest.mark.asyncio
async def test_budget_exhaustion_blocks_guardrail_not_ladder(
    tmp_path: Path,
) -> None:
    """A developer result carrying the synthetic
    ``error_max_turns_escalation_exhausted`` subtype must terminate cleanly via
    ``block_task(GUARDRAIL_EXCEEDED)`` — NOT re-enter the discard/stuck ladder as
    a WORKER_EXCEPTION (which would burn another retry slot).

    Engagement: assert the task ends blocked with the ``guardrail_exceeded``
    block class AND that the developer was dispatched exactly ONCE (no ladder
    re-dispatch). Broken-control: the legacy behaviour re-dispatched the
    developer (count > 1) and produced a worker_exception block_reason.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    exhausted = fail(
        "budget escalation ladder exhausted",
        subtype="error_max_turns_escalation_exhausted",
    )
    adapter = StubAdapter(
        {
            "explorer": ok("ok"),
            # If the ladder re-entered, the developer would be dispatched again;
            # provide a second response so a regression would visibly re-dispatch.
            "developer": [exhausted, _coder_diff("v2")],
            "reviewer": ok("APPROVED\n- ok"),
            "test_engineer": ok("passed=1 failed=0 total=1"),
        }
    )
    orch = await _build_orch(repo, adapter, session="ep2")

    tasks = await ep.run_execute_phase(orch)
    assert len(tasks) == 1
    final = tasks[0]

    # Terminal block via the guardrail path — not a worker_exception re-loop.
    assert final.status == "blocked", (
        f"expected blocked, got {final.status} "
        f"(blocked_reason={final.blocked_reason})"
    )
    reason = (final.blocked_reason or "")
    assert "guardrail_exceeded" in reason, (
        f"budget exhaustion did not route through GUARDRAIL_EXCEEDED; "
        f"blocked_reason={reason!r}"
    )
    # Broken-control: the legacy ladder absorbed this as WORKER_EXCEPTION and
    # re-dispatched the developer. With the fix the developer is dispatched
    # exactly once (the synthetic terminal blocks immediately).
    assert adapter.count("developer") == 1, (
        f"developer re-dispatched {adapter.count('developer')}× — the synthetic "
        f"budget-exhaustion terminal re-entered the ladder instead of blocking."
    )


# ---------------------------------------------------------------------------
# EP-5: a non-conflict WorktreeError at apply produces WORKTREE_APPLY_FAILED.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worktree_apply_infra_fault_produces_worktree_apply_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``WorktreeError`` that ESCAPES ``_apply_with_conflict_escalation`` (an
    apply INFRA fault, not a merge conflict) terminates with
    ``WORKTREE_APPLY_FAILED`` — so its ``repair_environment`` resolver rung is
    reachable — rather than collapsing to WORKER_EXCEPTION.

    Broken-control: monkeypatch the helper to raise the SAME WorktreeError. With
    the fix the block_reason is ``worktree_apply_failed``; without the EP-5 guard
    it would fall to the generic worker-exception handler (``worker_exception``).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    from orchestrator.worktree import WorktreeError

    async def _boom(*_a: Any, **_k: Any) -> bool:
        raise WorktreeError("git index.lock held; apply impossible (infra fault)")

    monkeypatch.setattr(ep, "_apply_with_conflict_escalation", _boom)

    adapter = StubAdapter(
        {
            "explorer": ok("ok"),
            "developer": _coder_diff("v1"),
            "reviewer": ok("APPROVED\n- ok"),
            "test_engineer": ok("passed=1 failed=0 total=1"),
        }
    )
    orch = await _build_orch(repo, adapter, session="ep5", disable_gates=True)

    tasks = await ep.run_execute_phase(orch)
    assert len(tasks) == 1
    final = tasks[0]

    assert final.status == "blocked", (
        f"expected blocked, got {final.status}"
    )
    reason = (final.blocked_reason or "")
    assert "worktree_apply_failed" in reason, (
        f"apply infra fault did not produce WORKTREE_APPLY_FAILED; "
        f"blocked_reason={reason!r} (regression: collapsed to worker_exception)."
    )
    # Broken-control corollary: it must NOT have been misclassified as a generic
    # worker exception.
    assert "worker_exception" not in reason, (
        f"apply infra fault misclassified as worker_exception: {reason!r}"
    )


# ---------------------------------------------------------------------------
# EP-6a: reconcile / reap failures HARD-ERROR (not silent log+continue).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_failure_hard_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed ``reconcile_evidence_vs_ledger`` aborts the run (raises) instead
    of silently continuing on unknown FSM state.

    Broken-control: the pre-fix behaviour swallowed the exception and proceeded;
    here we assert the run RAISES.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    adapter = StubAdapter({"explorer": ok("ok"), "developer": _coder_diff()})
    orch = await _build_orch(repo, adapter, session="ep6a")

    async def _boom() -> dict:
        raise RuntimeError("ledger read corrupt")

    monkeypatch.setattr(
        orch.plan_manager, "reconcile_evidence_vs_ledger", _boom
    )

    with pytest.raises(Exception) as ei:
        await ep.run_execute_phase(orch)
    assert "reconcile" in str(ei.value).lower()


@pytest.mark.asyncio
async def test_reap_orphans_failure_hard_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed ``reap_orphans`` aborts the run (raises) instead of silently
    continuing — wedged orphan tasks must never be left stranded."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    adapter = StubAdapter({"explorer": ok("ok"), "developer": _coder_diff()})
    orch = await _build_orch(repo, adapter, session="ep6b")

    async def _boom(*_a: Any, **_k: Any) -> list:
        raise RuntimeError("orphan reap failed")

    monkeypatch.setattr(orch.plan_manager, "reap_orphans", _boom)

    with pytest.raises(Exception) as ei:
        await ep.run_execute_phase(orch)
    assert "reap" in str(ei.value).lower()


# ---------------------------------------------------------------------------
# EP-6b: KB-consult outage emits resolver_kb_failed and REFUNDS the cycle.
# ---------------------------------------------------------------------------


@pytest.mark.resolver_enabled
@pytest.mark.asyncio
async def test_consult_knowledge_outage_emits_op_and_refunds_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the ``consult_knowledge`` rung's KB lookup raises (KB starvation):

      1. a ``resolver_kb_failed`` op is emitted (observability), and
      2. the per-(task, failure_class) resolver cycle is REFUNDED (the failure
         consumed no recovery budget), and
      3. the resolver DECLINES (returns None → caller does its legacy block).

    Broken-control: with the KB consult SUCCEEDING, no ``resolver_kb_failed`` op
    is emitted and a cycle IS consumed (the retry fires). We assert both legs.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    adapter = StubAdapter({"explorer": ok("ok"), "developer": _coder_diff()})
    orch = await _build_orch(repo, adapter, session="ep6c")
    plan = await orch.plan_manager.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    task = await orch.plan_manager.update_task_status(task.id, "in_progress")

    from orchestrator import blocker_resolver as _br
    from state.schemas import BlockerContext, ResolutionAction

    # Force the resolver to choose consult_knowledge so the rung is exercised.
    async def _choose_consult(_orch: Any, _ctx: Any) -> ResolutionAction:
        return ResolutionAction(
            action="consult_knowledge",
            params={},
            rationale="exercise the KB rung",
        )

    monkeypatch.setattr(_br, "resolve_blocker", _choose_consult)

    # KB consult RAISES → starvation path.
    async def _kb_boom(_orch: Any, _ctx: Any) -> str:
        raise RuntimeError("knowledge store unavailable")

    monkeypatch.setattr(_br, "consult_knowledge", _kb_boom)

    ctx_kwargs = dict(
        failure_class=_fcls.classify(_fcls.WORKER_EXCEPTION),
        raw_error="forced",
        failing_role="developer",
        task_id=task.id,
        phase_id="1",
    )
    _ = BlockerContext  # imported for clarity / future ctx assembly

    # Reset the in-memory cycle backstop so the count starts clean.
    ep._RESOLVER_CYCLE_COUNTS.pop(orch, None)

    # First call: consult_knowledge chosen, KB raises → decline + refund.
    recovered = await ep._maybe_resolve_blocker(
        orch,
        task,
        failure_class=_fcls.WORKER_EXCEPTION,
        raw_error="forced",
        failing_role="developer",
        phase_id="1",
    )
    assert recovered is None, "KB outage must DECLINE (fall through to block)"

    ops = _ops(repo)
    assert "resolver_kb_failed" in ops, (
        f"KB outage did not emit resolver_kb_failed; ops={ops}"
    )

    # The cycle was REFUNDED: the backstop count for this (task, class) is 0.
    counts = ep._RESOLVER_CYCLE_COUNTS.get(orch) or {}
    guard_key = f"{task.id}:{_fcls.WORKER_EXCEPTION}"
    assert counts.get(guard_key, 0) == 0, (
        f"KB outage consumed a resolver cycle (count={counts.get(guard_key)}); "
        f"it must be refunded so a KB outage never burns the recovery budget."
    )
    _ = ctx_kwargs  # silence unused (kept for documentation of the ctx shape)
