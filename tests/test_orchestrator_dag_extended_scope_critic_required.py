"""v0.20.0 C2: dag validator integration with critic review."""

from __future__ import annotations

import pytest

from orchestrator.dag import (
    EditScopeViolation,
    validate_edit_scope_with_critic_review,
)
from state.schemas import Phase, Plan, Task


def _plan_with_extended_task(files: list[str], extended_scope: list[str]) -> Plan:
    t = Task(
        id="1.1",
        phase_id="1",
        title="t",
        description="d",
        files=files,
        extended_scope=extended_scope,
    )
    return Plan(
        plan_id="p-1",
        spec_hash="x",
        phases=[Phase(id="1", title="P1", description="", tasks=[t])],
        edit_scope=["src/orch"],
        complexity="simple",
        created_at="2024-01-01T00:00:00+00:00",
        updated_at="2024-01-01T00:00:00+00:00",
    )


@pytest.mark.asyncio
async def test_critic_required_when_extended_scope_set() -> None:
    """Validator wrapper must invoke critic for tasks with extended_scope."""
    invocations = {"count": 0}

    async def fake_delegate(orch, role, env):
        invocations["count"] += 1

        class R:
            text = "RESOLUTION: approved-extended-scope\n"

        return R()

    class FakeOrch:
        plan_manager = None
        _extended_scope_delegate = staticmethod(fake_delegate)

    plan = _plan_with_extended_task(["src/foo/x.py"], ["src/foo"])
    await validate_edit_scope_with_critic_review(FakeOrch(), plan)
    assert invocations["count"] == 1


@pytest.mark.asyncio
async def test_critic_rejection_blocks_task() -> None:
    async def fake_delegate(orch, role, env):
        class R:
            text = "RESOLUTION: rejected-extended-scope\n"

        return R()

    class FakeOrch:
        plan_manager = None
        _extended_scope_delegate = staticmethod(fake_delegate)

    plan = _plan_with_extended_task(["src/foo/x.py"], ["src/foo"])
    with pytest.raises(EditScopeViolation):
        await validate_edit_scope_with_critic_review(FakeOrch(), plan)


@pytest.mark.asyncio
async def test_critic_decision_cached_across_calls() -> None:
    """Re-running the validator does NOT re-invoke the critic when cached."""
    invocations = {"count": 0}

    async def fake_delegate(orch, role, env):
        invocations["count"] += 1

        class R:
            text = "RESOLUTION: approved-extended-scope\n"

        return R()

    class FakePlan:
        def __init__(self, plan: Plan) -> None:
            self._plan = plan
            # Mirror the canonical Plan's metadata semantics
            self.metadata: dict = {}

    class FakePlanManager:
        def __init__(self, plan: Plan) -> None:
            self._plan = plan

        async def load(self):
            return self._plan

        async def save(self, plan):
            self._plan = plan

        async def ledger_append(self, op: str, payload: dict) -> None:
            pass

    class FakeOrch:
        def __init__(self, plan: Plan) -> None:
            self.plan_manager = FakePlanManager(plan)
            self._extended_scope_delegate = fake_delegate

    plan = _plan_with_extended_task(["src/foo/x.py"], ["src/foo"])
    orch = FakeOrch(plan)
    await validate_edit_scope_with_critic_review(orch, plan)
    await validate_edit_scope_with_critic_review(orch, plan)
    # Cached: critic invoked exactly once
    assert invocations["count"] == 1


@pytest.mark.asyncio
async def test_no_critic_invocation_when_extended_scope_empty() -> None:
    """Default (empty) extended_scope must not touch the critic."""
    invocations = {"count": 0}

    async def fake_delegate(orch, role, env):
        invocations["count"] += 1

        class R:
            text = "RESOLUTION: rejected-extended-scope\n"

        return R()

    class FakeOrch:
        plan_manager = None
        _extended_scope_delegate = staticmethod(fake_delegate)

    plan = _plan_with_extended_task(["src/orch/x.py"], [])
    await validate_edit_scope_with_critic_review(FakeOrch(), plan)
    assert invocations["count"] == 0
