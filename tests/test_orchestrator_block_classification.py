"""Tests for v0.29.0 Bug 6: orchestrator stamps typed
``Task.block_reason_class`` at every block site.

The four block sites covered:

  1. ``GuardrailExceededError`` in :func:`_execute_one` — the typed
     class depends on the *last* adapter result the worker saw. If
     the last result's ``subtype`` was ``auth_failed`` /
     ``rate_limited`` / ``server_error`` the guardrail tripped because
     the LLM was unavailable (infra). Otherwise the agent legitimately
     ate its budget (cap).
  2. QA-gate timeout / worker exception path in :func:`_execute_one_safe`
     — network/auth-class exceptions classify as infrastructure; all
     other exceptions classify as verdict.
  3. Architect-consult ``architect-infra`` action — always
     infrastructure (the architect explicitly diagnosed it).
  4. ``mark_blocked_descendants`` cascade — inherits the parent
     task's class (defaulting to ``"verdict"`` when the parent has
     none).
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

import pytest

from agents import build_registry
from config.defaults import default_config
from errors import GuardrailExceededError
from orchestrator import Orchestrator
from orchestrator import execute_phase as ep
from state.plan_manager import PlanManager
from state.schemas import AcceptanceCriterion, Phase, Plan, Task

from stub_adapter import StubAdapter, ok


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_single_task_plan() -> Plan:
    return Plan(
        plan_id="p-block-class",
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
        session_id="sess-test-block-class",
    )


@pytest.mark.asyncio
async def test_guardrail_with_auth_failed_subtype_classifies_as_infrastructure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the guardrail trips AND the most recent adapter result had
    ``subtype="auth_failed"``, the developer block site classifies as
    ``"infrastructure"`` — the loop didn't legitimately exhaust
    budget, the LLM was unavailable.

    Patches :func:`delegate` to (a) stash the adapter subtype on the
    orchestrator (mirroring what the real delegate does after every
    adapter call), and (b) raise the guardrail exception. The block
    site inside :func:`_execute_one` consumes the stashed subtype.
    """
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_single_task_plan())
    orch = _make_orch(tmp_path)

    async def _fake_delegate(
        orch_arg: Any,
        role: str,
        envelope: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        orch_arg._last_adapter_subtype = "auth_failed"
        raise GuardrailExceededError("budget exhausted on auth-failed retries")

    monkeypatch.setattr(ep, "delegate", _fake_delegate)

    await ep.run_execute_phase(orch)

    plan = await orch.plan_manager.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    assert task.status == "blocked"
    assert task.block_reason_class == "infrastructure"


@pytest.mark.asyncio
async def test_guardrail_with_max_turns_subtype_classifies_as_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the guardrail trips AND the most recent adapter result had
    ``subtype="error_max_turns"`` (or any non-infra subtype), the task
    block classifies as ``"cap"``: the agent legitimately consumed its
    budget on the work, requeueing without widening the cap won't help.
    """
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_single_task_plan())
    orch = _make_orch(tmp_path)

    async def _fake_delegate(
        orch_arg: Any,
        role: str,
        envelope: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        orch_arg._last_adapter_subtype = "error_max_turns"
        raise GuardrailExceededError("guardrail: ran out of turns legitimately")

    monkeypatch.setattr(ep, "delegate", _fake_delegate)

    await ep.run_execute_phase(orch)

    plan = await orch.plan_manager.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    assert task.status == "blocked"
    assert task.block_reason_class == "cap"


@pytest.mark.asyncio
async def test_qa_gate_timeout_classifies_as_infrastructure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``TimeoutError`` raised mid-QA-gate (the network/auth-class
    bucket of the worker-exception fallback) should classify as
    ``"infrastructure"`` — timeouts are typically transient.
    """
    import asyncio

    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_single_task_plan())
    orch = _make_orch(tmp_path)

    async def _raise_timeout(
        orch_arg: Any, task: Task, worktree_mgr: Any = None
    ) -> Task:
        await orch_arg.plan_manager.update_task_status(task.id, "in_progress")
        raise asyncio.TimeoutError("upstream gate timed out")

    monkeypatch.setattr(ep, "_execute_one", _raise_timeout)

    await ep.run_execute_phase(orch)

    plan = await orch.plan_manager.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    assert task.status == "blocked"
    assert task.block_reason_class == "infrastructure"


@pytest.mark.asyncio
async def test_architect_consult_infrastructure_classifies_as_infrastructure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``architect-infra`` arm of :func:`_dispatch_architect_consult`
    blocks the task with ``block_reason_class="infrastructure"``. We
    drive the dispatch directly with a stubbed architect response (the
    architect call itself is heavy and orthogonal to this assertion).
    """
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_single_task_plan())
    orch = _make_orch(tmp_path)

    # Walk the FSM into a state where blocked is reachable.
    await orch.plan_manager.update_task_status("1.1", "in_progress")

    plan = await orch.plan_manager.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]

    # Stub the architect call to return ``architect-infra``.
    async def _fake_architect(
        orch_arg: Any,
        task_arg: Task,
        *,
        stuck_state: object,
        ladder_step: str,
        recent_evidence: str,
        prior_attempts: list[str] | None,
    ) -> ep.ArchitectResolution:
        return ep.ArchitectResolution(
            action="architect-infra",
            guidance="upstream API returning 503 sustained",
        )

    monkeypatch.setattr(ep, "_escalate_stuck_to_architect", _fake_architect)

    from orchestrator.escalation_ladder import StuckState

    blocked = await ep._dispatch_architect_consult(
        orch,
        task,
        stuck_state=StuckState(),
        reason="developer kept failing",
        prior_attempts=None,
        web_context_block="",
    )
    assert blocked is not None
    assert blocked.status == "blocked"
    assert blocked.block_reason_class == "infrastructure"


@pytest.mark.asyncio
async def test_upstream_cascade_inherits_parent_class(
    tmp_path: Path,
) -> None:
    """When :meth:`PlanManager.mark_blocked_descendants` cascades the
    block from a failed parent to its pending descendants, every
    descendant inherits the parent's ``block_reason_class``. Use a
    parent classified as ``"infrastructure"`` and a cascade reason that
    contains NO infra keywords (so the migration shim cannot
    accidentally land the same answer via fallback) — the assertion
    must specifically verify the cascade stamp.
    """
    plan = Plan(
        plan_id="p-cascade",
        spec_hash="0123456789abcdef",
        phases=[
            Phase(
                id="1",
                title="Cascade",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="parent",
                        description="parent",
                    ),
                    Task(
                        id="1.2",
                        phase_id="1",
                        title="child",
                        description="child",
                        depends_on=["1.1"],
                    ),
                ],
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
    )
    pm = PlanManager(tmp_path, session_id="sess-cascade")
    await pm.init_plan(plan)

    # Walk parent through the FSM and stamp it as infrastructure-
    # blocked with a free-text reason that does NOT match the keyword
    # heuristic (no 401/403/auth/etc). This way the cascade descendant
    # can only have ``block_reason_class="infrastructure"`` if the
    # cascade explicitly stamped it (the migration shim would fall
    # back to ``"verdict"``).
    await pm.update_task_status("1.1", "in_progress")
    await pm.update_task_status(
        "1.1",
        "blocked",
        meta={
            "blocked_reason": "external service down for sustained period",
            "block_reason_class": "infrastructure",
        },
    )

    cascaded = await pm.mark_blocked_descendants(
        "1", "1.1", "parent went infra"
    )
    assert cascaded == ["1.2"]

    reloaded = await pm.load()
    assert reloaded is not None
    child = reloaded.phases[0].tasks[1]
    assert child.status == "blocked"
    assert child.block_reason_class == "infrastructure"
