"""v0.14.0 — developer prompt EDIT SCOPE injection.

When a plan declares a non-empty ``Plan.edit_scope``, the developer
delegation MUST include an ``EDIT SCOPE: <prefixes>`` line in its
prompt so the LLM is aware of the boundary it is expected to respect.
The injection is gated on ``role == "developer"`` AND non-empty
resolved scope (Phase override → plan scope → empty fallback).

We assert the rendered prompt — not the LLM's behavior — by sniffing
the StubAdapter's most-recent ``last_invocation`` after the developer
delegation completes.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator.delegation_envelope import DelegationEnvelope
from orchestrator.execute_phase import delegate
from state.schemas import (
    AcceptanceCriterion,
    Phase,
    Plan,
    Task,
)

from stub_adapter import StubAdapter, ok


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _make_plan_with_scope(
    plan_scope: list[str] | None = None,
    phase_scope: list[str] | None = None,
) -> Plan:
    return Plan(
        plan_id="p-dev-scope",
        spec_hash="d",
        phases=[
            Phase(
                id="1",
                title="Work",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="Add subtract",
                        description="Implement subtract(a, b)",
                        files=["src/math.py"],
                        acceptance=[
                            AcceptanceCriterion(id="ac-1", description="tests pass"),
                        ],
                    )
                ],
                edit_scope=phase_scope,
            )
        ],
        edit_scope=plan_scope or [],
        created_at=_iso(),
        updated_at=_iso(),
    )


async def _make_orch(
    cwd: Path,
    adapter: StubAdapter,
    plan: Plan,
) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    registry = build_registry(cfg)
    orch = Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-dev-scope",
    )
    await orch.plan_manager.init_plan(plan)
    return orch


@pytest.mark.asyncio
async def test_developer_prompt_includes_edit_scope_when_plan_scope_set(
    tmp_path: Path,
) -> None:
    """When ``Plan.edit_scope`` is non-empty, the developer prompt
    contains an ``EDIT SCOPE:`` line listing the prefixes."""
    plan = _make_plan_with_scope(plan_scope=["src/math", "tests/math"])
    adapter = StubAdapter({"developer": ok("done")})
    orch = await _make_orch(tmp_path, adapter, plan)

    env = DelegationEnvelope(
        task_id="1.1",
        target_agent="developer",
        action="implement",
        acceptance="ok",
        context={},
    )
    await delegate(orch, "developer", env, task=plan.phases[0].tasks[0])

    last = adapter.calls[-1]
    assert "EDIT SCOPE" in last.prompt
    assert "src/math" in last.prompt
    assert "tests/math" in last.prompt


@pytest.mark.asyncio
async def test_developer_prompt_uses_phase_scope_override(tmp_path: Path) -> None:
    """Phase-level scope override is what flows into the developer prompt
    when set."""
    plan = _make_plan_with_scope(
        plan_scope=["src"],
        phase_scope=["src/math"],
    )
    adapter = StubAdapter({"developer": ok("done")})
    orch = await _make_orch(tmp_path, adapter, plan)

    env = DelegationEnvelope(
        task_id="1.1",
        target_agent="developer",
        action="implement",
        acceptance="ok",
        context={},
    )
    await delegate(orch, "developer", env, task=plan.phases[0].tasks[0])

    last = adapter.calls[-1]
    assert "EDIT SCOPE" in last.prompt
    assert "src/math" in last.prompt
    # The narrower phase scope wins; a broader plan-only "src" without
    # the "/math" suffix would not appear standalone.
    # We can verify by absence of a bare "src," / "src\n" pattern that's
    # not part of "src/math". Simpler: the phase_scope is exactly one
    # entry and the line should equal "EDIT SCOPE: src/math".
    assert "EDIT SCOPE: src/math" in last.prompt


@pytest.mark.asyncio
async def test_developer_prompt_no_edit_scope_when_plan_scope_empty(
    tmp_path: Path,
) -> None:
    """No EDIT SCOPE line is injected when ``Plan.edit_scope`` is empty
    (legacy behavior preserved)."""
    plan = _make_plan_with_scope(plan_scope=[])
    adapter = StubAdapter({"developer": ok("done")})
    orch = await _make_orch(tmp_path, adapter, plan)

    env = DelegationEnvelope(
        task_id="1.1",
        target_agent="developer",
        action="implement",
        acceptance="ok",
        context={},
    )
    await delegate(orch, "developer", env, task=plan.phases[0].tasks[0])

    last = adapter.calls[-1]
    assert "EDIT SCOPE:" not in last.prompt


@pytest.mark.asyncio
async def test_non_developer_role_does_not_inject_edit_scope(tmp_path: Path) -> None:
    """The EDIT SCOPE line is gated on the developer role only — reviewer
    and other roles continue to receive their existing prompts unchanged."""
    plan = _make_plan_with_scope(plan_scope=["src"])
    adapter = StubAdapter({"reviewer": ok("APPROVED\n- ok")})
    orch = await _make_orch(tmp_path, adapter, plan)

    env = DelegationEnvelope(
        task_id="1.1",
        target_agent="reviewer",
        action="review",
        acceptance="ok",
        context={},
    )
    await delegate(orch, "reviewer", env, task=plan.phases[0].tasks[0])

    last = adapter.calls[-1]
    assert "EDIT SCOPE:" not in last.prompt
