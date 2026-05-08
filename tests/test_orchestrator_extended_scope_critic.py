"""v0.20.0 C2: extended_scope_critic module tests."""

from __future__ import annotations

from typing import Any

import pytest

from orchestrator.extended_scope_critic import (
    _parse_resolution,
    critic_review_extended_scope,
    scope_signature,
)
from state.schemas import Task


def _task(extended_scope: list[str] | None = None, **overrides: Any) -> Task:
    return Task(
        id=overrides.pop("id", "1.1"),
        phase_id=overrides.pop("phase_id", "1"),
        title="t",
        description="d",
        files=overrides.pop("files", []),
        extended_scope=extended_scope or [],
        **overrides,
    )


def test_scope_signature_stable_for_same_scope() -> None:
    t1 = _task(extended_scope=["src/foo", "src/bar"])
    t2 = _task(extended_scope=["src/bar", "src/foo"])  # different order
    # Sorted internally; signature stable across orders
    assert scope_signature(t1) == scope_signature(t2)


def test_scope_signature_differs_when_scope_differs() -> None:
    t1 = _task(extended_scope=["src/foo"])
    t2 = _task(extended_scope=["src/bar"])
    assert scope_signature(t1) != scope_signature(t2)


def test_parse_resolution_approval_token() -> None:
    text = "Looks fine.\n\nRESOLUTION: approved-extended-scope\n"
    assert _parse_resolution(text) is True


def test_parse_resolution_rejection_token() -> None:
    text = "Vague justification.\nRESOLUTION: rejected-extended-scope\n"
    assert _parse_resolution(text) is False


def test_parse_resolution_no_token_returns_none() -> None:
    text = "I'm not sure how to vote."
    assert _parse_resolution(text) is None


def test_parse_resolution_rejection_wins_over_approval() -> None:
    """Defensive: if both tokens appear, rejection wins (fail-closed)."""
    text = (
        "RESOLUTION: approved-extended-scope\n"
        "RESOLUTION: rejected-extended-scope\n"
    )
    assert _parse_resolution(text) is False


@pytest.mark.asyncio
async def test_critic_review_returns_true_on_empty_extended_scope() -> None:
    """No critic invocation when extended_scope is empty."""

    class FakeOrch:
        plan_manager = None

    t = _task(extended_scope=[])
    approved = await critic_review_extended_scope(FakeOrch(), t)
    assert approved is True


@pytest.mark.asyncio
async def test_critic_review_uses_custom_delegate_when_set() -> None:
    """An ``_extended_scope_delegate`` attr on orch is used in place of
    the real ``execute_phase.delegate``."""
    captured: dict[str, Any] = {}

    async def fake_delegate(orch, role, env):
        captured["role"] = role
        captured["env"] = env

        class R:
            text = "RESOLUTION: approved-extended-scope\n"

        return R()

    class FakeOrch:
        plan_manager = None
        _extended_scope_delegate = staticmethod(fake_delegate)

    t = _task(extended_scope=["src/foo"])
    approved = await critic_review_extended_scope(FakeOrch(), t)
    assert approved is True
    assert captured["role"] == "critic_sounding_board"


@pytest.mark.asyncio
async def test_critic_review_returns_false_on_rejected_resolution() -> None:
    async def fake_delegate(orch, role, env):
        class R:
            text = "RESOLUTION: rejected-extended-scope\n"

        return R()

    class FakeOrch:
        plan_manager = None
        _extended_scope_delegate = staticmethod(fake_delegate)

    t = _task(extended_scope=["src/foo"])
    approved = await critic_review_extended_scope(FakeOrch(), t)
    assert approved is False


@pytest.mark.asyncio
async def test_critic_review_fails_closed_on_no_resolution() -> None:
    """Missing resolution token → rejection (fail-closed)."""

    async def fake_delegate(orch, role, env):
        class R:
            text = "I forgot to vote."

        return R()

    class FakeOrch:
        plan_manager = None
        _extended_scope_delegate = staticmethod(fake_delegate)

    t = _task(extended_scope=["src/foo"])
    approved = await critic_review_extended_scope(FakeOrch(), t)
    assert approved is False


@pytest.mark.asyncio
async def test_critic_review_fails_closed_on_delegate_error() -> None:
    async def fake_delegate(orch, role, env):
        raise RuntimeError("boom")

    class FakeOrch:
        plan_manager = None
        _extended_scope_delegate = staticmethod(fake_delegate)

    t = _task(extended_scope=["src/foo"])
    approved = await critic_review_extended_scope(FakeOrch(), t)
    assert approved is False


@pytest.mark.asyncio
async def test_critic_review_caches_decision_in_plan_manager_metadata() -> None:
    """Second invocation hits the cache (delegate not called twice)."""
    invocations = {"count": 0}

    async def fake_delegate(orch, role, env):
        invocations["count"] += 1

        class R:
            text = "RESOLUTION: approved-extended-scope\n"

        return R()

    class FakePlan:
        def __init__(self) -> None:
            self.metadata: dict = {}

    class FakePlanManager:
        def __init__(self) -> None:
            self._plan = FakePlan()

        async def load(self):
            return self._plan

        async def save(self, plan):
            self._plan = plan

        async def ledger_append(self, op: str, payload: dict) -> None:
            pass

    class FakeOrch:
        def __init__(self) -> None:
            self.plan_manager = FakePlanManager()
            self._extended_scope_delegate = fake_delegate

    orch = FakeOrch()
    t = _task(extended_scope=["src/foo"])
    a1 = await critic_review_extended_scope(orch, t)
    a2 = await critic_review_extended_scope(orch, t)
    assert a1 is True
    assert a2 is True
    # Delegate invoked exactly once due to cache hit on second call
    assert invocations["count"] == 1


@pytest.mark.asyncio
async def test_validate_with_critic_review_admits_approved_extension() -> None:
    """The async validator wrapper passes when critic approves."""
    from orchestrator.dag import validate_edit_scope_with_critic_review
    from state.schemas import Phase, Plan

    async def fake_delegate(orch, role, env):
        class R:
            text = "RESOLUTION: approved-extended-scope\n"

        return R()

    class FakeOrch:
        plan_manager = None
        _extended_scope_delegate = staticmethod(fake_delegate)

    t = Task(
        id="1.1",
        phase_id="1",
        title="t",
        description="d",
        files=["src/foo/x.py"],
        extended_scope=["src/foo"],
    )
    plan = Plan(
        plan_id="p-1",
        spec_hash="x",
        phases=[Phase(id="1", title="P1", description="", tasks=[t])],
        edit_scope=["src/orch"],
        complexity="simple",
        created_at="2024-01-01T00:00:00+00:00",
        updated_at="2024-01-01T00:00:00+00:00",
    )
    # Should not raise
    await validate_edit_scope_with_critic_review(FakeOrch(), plan)


@pytest.mark.asyncio
async def test_validate_with_critic_review_rejects_unapproved_extension() -> None:
    from orchestrator.dag import (
        EditScopeViolation,
        validate_edit_scope_with_critic_review,
    )
    from state.schemas import Phase, Plan

    async def fake_delegate(orch, role, env):
        class R:
            text = "RESOLUTION: rejected-extended-scope\n"

        return R()

    class FakeOrch:
        plan_manager = None
        _extended_scope_delegate = staticmethod(fake_delegate)

    t = Task(
        id="1.1",
        phase_id="1",
        title="t",
        description="d",
        files=["src/foo/x.py"],
        extended_scope=["src/foo"],
    )
    plan = Plan(
        plan_id="p-1",
        spec_hash="x",
        phases=[Phase(id="1", title="P1", description="", tasks=[t])],
        edit_scope=["src/orch"],
        complexity="simple",
        created_at="2024-01-01T00:00:00+00:00",
        updated_at="2024-01-01T00:00:00+00:00",
    )
    with pytest.raises(EditScopeViolation):
        await validate_edit_scope_with_critic_review(FakeOrch(), plan)


@pytest.mark.asyncio
async def test_validate_with_critic_review_skips_when_no_extended_scope() -> None:
    """Tasks without extended_scope never invoke the critic."""
    from orchestrator.dag import validate_edit_scope_with_critic_review
    from state.schemas import Phase, Plan

    invocations = {"count": 0}

    async def fake_delegate(orch, role, env):
        invocations["count"] += 1

        class R:
            text = "RESOLUTION: rejected-extended-scope\n"

        return R()

    class FakeOrch:
        plan_manager = None
        _extended_scope_delegate = staticmethod(fake_delegate)

    t = Task(
        id="1.1",
        phase_id="1",
        title="t",
        description="d",
        files=["src/orch/x.py"],
    )
    plan = Plan(
        plan_id="p-1",
        spec_hash="x",
        phases=[Phase(id="1", title="P1", description="", tasks=[t])],
        edit_scope=["src/orch"],
        complexity="simple",
        created_at="2024-01-01T00:00:00+00:00",
        updated_at="2024-01-01T00:00:00+00:00",
    )
    await validate_edit_scope_with_critic_review(FakeOrch(), plan)
    assert invocations["count"] == 0
