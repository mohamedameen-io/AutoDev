"""Tests for orchestrator suspend/resume with InlineAdapter."""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from adapters.inline import InlineAdapter
from adapters.inline_types import DelegationPendingSignal, InlineResponseFile
from agents import build_registry
from config.defaults import default_config
from errors import AutodevError
from orchestrator import Orchestrator
from orchestrator.inline_state import (
    load_suspend_state,
    write_suspend_state,
)
from state.paths import inline_state_path, response_path
from state.schemas import (
    AcceptanceCriterion,
    Phase,
    Plan,
    Task,
)

from stub_adapter import StubAdapter, ok


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan(task_id: str = "1.1") -> Plan:
    return Plan(
        plan_id="p-inline",
        spec_hash="abc",
        phases=[
            Phase(
                id="1",
                title="Work",
                tasks=[
                    Task(
                        id=task_id,
                        phase_id="1",
                        title="Add subtract",
                        description="Implement subtract(a, b)",
                        files=["math.py"],
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


async def _make_inline_orch(cwd: Path) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    cfg.qa_gates.syntax_check = False
    cfg.qa_gates.lint = False
    cfg.qa_gates.build_check = False
    cfg.qa_gates.secretscan = False
    registry = build_registry(cfg)
    adapter = InlineAdapter(cwd=cwd)
    orch = Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-inline",
    )
    await orch.plan_manager.init_plan(_mk_plan())
    return orch


def _write_response(
    cwd: Path,
    task_id: str,
    role: str,
    *,
    success: bool = True,
    text: str = "Done.",
) -> None:
    resp = InlineResponseFile(
        task_id=task_id,
        role=role,
        success=success,
        text=text,
    )
    p = response_path(cwd, task_id, role)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(resp.model_dump_json(), encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. execute() raises DelegationPendingSignal and writes inline-state.json
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_suspends_on_delegation_signal(tmp_path: Path) -> None:
    orch = await _make_inline_orch(tmp_path)

    with pytest.raises(DelegationPendingSignal):
        await orch.execute()

    state = load_suspend_state(tmp_path)
    assert state is not None
    assert state.pending_task_id == "1.1"
    assert state.pending_role == "developer"
    assert state.session_id == "sess-inline"
    assert state.orchestrator_step == "developer"


# ---------------------------------------------------------------------------
# 2. resume() raises AutodevError when response file doesn't exist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_raises_if_no_response_file(tmp_path: Path) -> None:
    orch = await _make_inline_orch(tmp_path)

    # Manually write a suspend state without a response file.
    write_suspend_state(
        cwd=tmp_path,
        session_id="sess-inline",
        pending_task_id="1.1",
        pending_role="developer",
        delegation_path=tmp_path / ".autodev" / "delegations" / "1.1-coder.md",
        response_path=tmp_path / ".autodev" / "responses" / "1.1-developer.json",
        orchestrator_step="developer",
    )

    with pytest.raises(AutodevError, match="Response file not yet written"):
        await orch.resume()


# ---------------------------------------------------------------------------
# 3. resume() clears state and continues when response file exists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_continues_with_valid_response(tmp_path: Path) -> None:
    orch = await _make_inline_orch(tmp_path)

    # Write suspend state.
    write_suspend_state(
        cwd=tmp_path,
        session_id="sess-inline",
        pending_task_id="1.1",
        pending_role="developer",
        delegation_path=tmp_path / ".autodev" / "delegations" / "1.1-developer.md",
        response_path=tmp_path / ".autodev" / "responses" / "1.1-developer.json",
        orchestrator_step="developer",
    )

    # Write developer response.
    _write_response(tmp_path, "1.1", "developer", text="DONE: implemented subtract")

    # resume() should clear state and re-enter the loop.
    # The loop will then try to call reviewer (inline) → raises DelegationPendingSignal.
    with pytest.raises(DelegationPendingSignal) as exc_info:
        await orch.resume()

    # Inline state should have been cleared then re-written for reviewer.
    state = load_suspend_state(tmp_path)
    assert state is not None
    assert state.pending_role == "reviewer"

    sig = exc_info.value
    assert sig.role == "reviewer"


# ---------------------------------------------------------------------------
# 4. delegate() shortcut: collects existing response without re-delegating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delegate_collects_existing_response(tmp_path: Path) -> None:
    from orchestrator.execute_phase import delegate
    from orchestrator.delegation_envelope import DelegationEnvelope

    orch = await _make_inline_orch(tmp_path)

    # Pre-write a response file.
    _write_response(tmp_path, "1.1", "developer", text="pre-existing response")

    env = DelegationEnvelope(
        task_id="1.1",
        target_agent="developer",
        action="implement",
        context={"task_title": "test", "task_description": "test"},
    )
    result = await delegate(orch, "developer", env)

    assert result.success is True
    assert result.text == "pre-existing response"
    # No delegation file should have been written (no signal raised).
    del_path = tmp_path / ".autodev" / "delegations" / "1.1-coder.md"
    assert not del_path.exists()


# ---------------------------------------------------------------------------
# 5. Multi-step ping-pong: coder → suspend → resume → reviewer → suspend → resume → complete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_step_ping_pong(tmp_path: Path) -> None:
    orch = await _make_inline_orch(tmp_path)

    # Step 1: execute() suspends at coder.
    with pytest.raises(DelegationPendingSignal) as exc1:
        await orch.execute()
    assert exc1.value.role == "developer"
    assert load_suspend_state(tmp_path) is not None

    # Agent writes developer response.
    _write_response(
        tmp_path,
        "1.1",
        "developer",
        text="DIFF:\n--- a/x.py\n+++ b/x.py\n@@ -0,0 +1,1 @@\n+pass\n",
    )

    # Step 2: resume() clears state, collects developer response, suspends at reviewer.
    with pytest.raises(DelegationPendingSignal) as exc2:
        await orch.resume()
    assert exc2.value.role == "reviewer"
    state2 = load_suspend_state(tmp_path)
    assert state2 is not None
    assert state2.pending_role == "reviewer"

    # Agent writes reviewer response.
    _write_response(tmp_path, "1.1", "reviewer", text="APPROVED\n- looks good")

    # Step 3: resume() clears state, collects reviewer response, suspends at test_engineer.
    with pytest.raises(DelegationPendingSignal) as exc3:
        await orch.resume()
    assert exc3.value.role == "test_engineer"

    # Agent writes test_engineer response.
    _write_response(
        tmp_path, "1.1", "test_engineer", text="RESULTS: passed=3 failed=0 total=3"
    )

    # Step 4: resume() completes the task.
    tasks = await orch.resume()
    assert len(tasks) == 1
    assert tasks[0].status == "complete"
    # Inline state should be gone.
    assert load_suspend_state(tmp_path) is None


# ---------------------------------------------------------------------------
# 6. StubAdapter never raises DelegationPendingSignal (regression test)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subprocess_adapter_never_raises_signal(tmp_path: Path) -> None:
    """StubAdapter (subprocess path) must never raise DelegationPendingSignal."""
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    cfg.qa_gates.syntax_check = False
    cfg.qa_gates.lint = False
    cfg.qa_gates.build_check = False
    cfg.qa_gates.secretscan = False
    registry = build_registry(cfg)

    adapter = StubAdapter(
        {
            "developer": ok(
                "DONE",
                diff="diff --git a/math.py b/math.py\n+def subtract(a,b): return a-b",
                files_changed=[Path("math.py")],
            ),
            "reviewer": ok("APPROVED\n- clean"),
            "test_engineer": ok("RESULTS: passed=3 failed=0 total=3"),
        }
    )
    orch = Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-stub",
    )
    await orch.plan_manager.init_plan(_mk_plan())

    # Should complete without raising DelegationPendingSignal.
    tasks = await orch.execute()
    assert tasks[0].status == "complete"
    # No inline state file written.
    assert not inline_state_path(tmp_path).exists()
