"""Tests for v0.13.0 ``repo_capacity`` threading through ``delegate``.

When ``orch._repo_capacity.is_huge`` is True, ``delegate`` resolves the
developer's ``max_turns`` via :func:`tournament.task_overrides.resolve_task_max_turns`
with the ``capacity`` argument, doubling the per-task budget so genuinely
complex tasks have runway on Unity-class repos.

These tests construct a minimal orchestrator stub with a fake adapter that
records the AgentInvocation it received, then assert ``inv.max_turns``
matches the expected scaled value.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from adapters.types import AgentInvocation, AgentResult, AgentSpec
from orchestrator.delegation_envelope import DelegationEnvelope
from orchestrator.execute_phase import delegate
from runtime.repo_probe import RepoCapacity
from state.schemas import Task


def _build_orch_stub(
    tmp_path: Path,
    *,
    capacity: RepoCapacity | None,
    captured: dict,
) -> object:
    """Construct an Orchestrator-shaped stub for delegate() exercise."""

    class FakeAdapter:
        async def execute(self, inv: AgentInvocation) -> AgentResult:
            captured["max_turns"] = inv.max_turns
            captured["timeout_s"] = inv.timeout_s
            return AgentResult(
                text="ok",
                success=True,
                duration_s=0.1,
                files_changed=[],
                diff="",
            )

    class FakeKnowledge:
        async def inject_block(
            self, role: str, task_id: str | None = None
        ) -> str:
            return ""

    class FakeRegistry:
        def get(self, role: str) -> AgentSpec:
            return AgentSpec(
                name=role,
                model="sonnet",
                prompt="you are a developer",
                description="",
                tools=[],
                max_turns=10,
            )

    class FakeGuardrails:
        def start_execute_phase(self, *a, **k):
            return None

        def execute_phase_wall_budget_exceeded(self, *a, **k):
            return False

        def check_execute_phase_wall_budget(self, *a, **k):
            return None

        def pre_invocation(self, *_a, **_k):
            pass

        def post_invocation(self, *_a, **_k):
            pass

    class FakeLoop:
        def observe(self, *_a, **_k):
            pass

    class FakePlanManager:
        async def load(self):
            return None

    return type(
        "OrchStub",
        (),
        {
            "registry": FakeRegistry(),
            "knowledge": FakeKnowledge(),
            "cfg": type(
                "CfgStub",
                (),
                {
                    "agents": {},
                    "user_complexity": "medium",
                },
            )(),
            "plan_manager": FakePlanManager(),
            "adapter": FakeAdapter(),
            "guardrails": FakeGuardrails(),
            "loop_detector": FakeLoop(),
            "cwd": tmp_path,
            "session_id": "s1",
            # v0.13.0: the new capacity slot read by delegate.
            "_repo_capacity": capacity,
        },
    )()


@pytest.mark.asyncio
async def test_delegate_passes_capacity_into_resolve_task_max_turns(
    tmp_path: Path,
) -> None:
    """v0.20.0 D1: simple+huge → 30 (3.0× per-bucket curve replaces legacy 2.0×)."""
    huge_cap = RepoCapacity(
        file_count=25_000, total_bytes=10_000_000_000, depth_max=10, is_huge=True
    )
    captured: dict = {}
    orch = _build_orch_stub(tmp_path, capacity=huge_cap, captured=captured)

    task = Task(
        id="1.1", phase_id="1", title="t", description="d", complexity="simple"
    )
    env = DelegationEnvelope(
        task_id="1.1",
        target_agent="developer",
        action="implement",
        files=[],
        context={},
    )
    await delegate(orch, "developer", env, task=task)
    assert captured["max_turns"] == 30


@pytest.mark.asyncio
async def test_delegate_with_normal_capacity_preserves_legacy_max_turns(
    tmp_path: Path,
) -> None:
    """``is_huge=False`` → legacy v0.12.0 lookup table value (simple → 10)."""
    normal_cap = RepoCapacity(
        file_count=1_000, total_bytes=10_000_000, depth_max=5, is_huge=False
    )
    captured: dict = {}
    orch = _build_orch_stub(tmp_path, capacity=normal_cap, captured=captured)

    task = Task(
        id="1.1", phase_id="1", title="t", description="d", complexity="simple"
    )
    env = DelegationEnvelope(
        task_id="1.1",
        target_agent="developer",
        action="implement",
        files=[],
        context={},
    )
    await delegate(orch, "developer", env, task=task)
    assert captured["max_turns"] == 10


@pytest.mark.asyncio
async def test_delegate_with_no_capacity_preserves_legacy_max_turns(
    tmp_path: Path,
) -> None:
    """``orch._repo_capacity=None`` (probe never ran) → legacy lookup."""
    captured: dict = {}
    orch = _build_orch_stub(tmp_path, capacity=None, captured=captured)

    task = Task(
        id="1.1", phase_id="1", title="t", description="d", complexity="medium"
    )
    env = DelegationEnvelope(
        task_id="1.1",
        target_agent="developer",
        action="implement",
        files=[],
        context={},
    )
    await delegate(orch, "developer", env, task=task)
    assert captured["max_turns"] == 20


@pytest.mark.asyncio
async def test_delegate_huge_cap_with_complex_task_per_bucket_curve(
    tmp_path: Path,
) -> None:
    """v0.20.0 D1: complex bucket gets 1.5× (was 2.0×) — 40 → 60."""
    huge_cap = RepoCapacity(
        file_count=25_000, total_bytes=10_000_000_000, depth_max=10, is_huge=True
    )
    captured: dict = {}
    orch = _build_orch_stub(tmp_path, capacity=huge_cap, captured=captured)

    task = Task(
        id="1.1", phase_id="1", title="t", description="d", complexity="complex"
    )
    env = DelegationEnvelope(
        task_id="1.1",
        target_agent="developer",
        action="implement",
        files=[],
        context={},
    )
    await delegate(orch, "developer", env, task=task)
    assert captured["max_turns"] == 60
