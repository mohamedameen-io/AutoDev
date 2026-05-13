"""Tests for v0.30.0 Bug 4: ledger logs ``api_error_status`` for
post-mortems + new ``adapter_failure`` audit op.

Two assertions covered:

  1. ``test_block_records_api_error_status_in_ledger`` — when the
     orchestrator blocks a task and the meta carries
     ``api_error_status`` / ``last_adapter_subtype``, the
     ``update_task_status`` ledger payload preserves both keys
     verbatim. Forensics can then reconstruct "the block was
     triggered by a 403 from auth_failed" without grepping
     ``.autodev/debug/*.txt``.
  2. ``test_adapter_failure_op_appended_on_transient`` — every
     adapter result with ``success=False`` (regardless of whether
     the worker eventually retries or blocks) emits a separate
     ``op="adapter_failure"`` audit entry carrying the
     ``task_id``, ``api_error_status``, ``subtype``, ``error``,
     and ``attempt_n`` fields. Best-effort: ledger write failures
     do NOT mask the underlying adapter failure.
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

from stub_adapter import StubAdapter, fail, ok


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_single_task_plan() -> Plan:
    return Plan(
        plan_id="p-ledger-api-status",
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
        session_id="sess-test-ledger-api",
    )


@pytest.mark.asyncio
async def test_block_records_api_error_status_in_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the developer block site fires AND the orchestrator's
    most recent adapter result carried ``api_error_status=403`` /
    ``subtype="auth_failed"``, the ``update_task_status`` ledger
    entry's payload includes BOTH keys verbatim. A forensic walk
    can reconstruct the API status without diving into
    ``.autodev/debug/``.
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
        # Mirror what the real :func:`delegate` does after every
        # adapter call (v0.29.0 stash on the orchestrator).
        orch_arg._last_adapter_subtype = "auth_failed"
        orch_arg._last_adapter_api_error_status = 403
        raise GuardrailExceededError("budget exhausted on auth-failed retries")

    monkeypatch.setattr(ep, "delegate", _fake_delegate)

    await ep.run_execute_phase(orch)

    entries = await orch.plan_manager.read_ledger()
    # Find the update_task_status entry that transitions 1.1 to blocked.
    block_entries = [
        e
        for e in entries
        if e.op == "update_task_status"
        and e.payload.get("task_id") == "1.1"
        and e.payload.get("status") == "blocked"
    ]
    assert block_entries, "expected at least one blocked update_task_status entry"
    payload = block_entries[-1].payload
    assert payload.get("api_error_status") == 403
    assert payload.get("last_adapter_subtype") == "auth_failed"


@pytest.mark.asyncio
async def test_adapter_failure_op_appended_on_transient(
    tmp_path: Path,
) -> None:
    """When the adapter returns ``success=False`` (a transient
    failure that the orchestrator's retry layer would normally
    react to), the ``delegate()`` hook appends an
    ``op="adapter_failure"`` audit entry carrying the task id,
    api_error_status, subtype, error, and attempt_n keys.
    Forensics can grep the ledger directly for transient failures
    without inspecting ``.autodev/debug/`` traceback files.
    """
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_single_task_plan())

    cfg = default_config()
    cfg.tournaments.phase_review.enabled = False
    cfg.tournaments.auto_disable_for_models = []
    cfg.tournaments.impl.enabled = False
    cfg.tournaments.plan.enabled = False
    registry = build_registry(cfg)
    # Adapter returns a transient 503 server_error. The orchestrator's
    # retry layer will handle it; the audit op should be appended
    # regardless of the retry outcome.
    bad = fail(
        "upstream returned 503",
        subtype="server_error",
        api_error_status=503,
    )
    adapter = StubAdapter({"developer": bad})
    orch = Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-test-adapter-failure",
    )

    # Drive a single delegate call directly — sufficient to assert the
    # ledger op is appended on adapter failure. Avoids dragging in the
    # full retry / block FSM (orthogonal to this assertion).
    plan = await orch.plan_manager.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    await orch.plan_manager.update_task_status(task.id, "in_progress")

    env = ep._developer_envelope(task, extra_issues=[])
    result = await ep.delegate(
        orch,
        "developer",
        env,
        retry_count=0,
        last_issues=None,
        task=task,
    )
    assert not result.success

    entries = await orch.plan_manager.read_ledger()
    failure_entries = [e for e in entries if e.op == "adapter_failure"]
    assert failure_entries, "expected at least one adapter_failure ledger entry"
    payload = failure_entries[-1].payload
    assert payload.get("task_id") == task.id
    assert payload.get("api_error_status") == 503
    assert payload.get("subtype") == "server_error"
    assert payload.get("error") == "upstream returned 503"
    # attempt_n is the developer's retry_count at dispatch time (0 here).
    assert payload.get("attempt_n") == 0
