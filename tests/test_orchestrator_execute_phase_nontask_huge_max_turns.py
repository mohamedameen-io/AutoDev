"""Tests for v0.39.0 (Cluster C1) non-task-role huge-repo max_turns scaling.

Non-task roles (reviewer, test_engineer, domain_expert, critics, …) hit the
``else`` branch of ``delegate``'s per-call ``max_turns`` resolution — they
have no ``Task`` so the per-task complexity resolver is skipped. Before
v0.39.0 that branch pinned ``max_turns = spec.max_turns`` regardless of repo
size, which is why the reviewer needed a manual ``reviewer.max_turns=12``
override to survive Unity-class runs.

C1 scales these roles by the role-keyed ``task_overrides.huge_repo_multipliers``
dict, gated on the same ``_repo_capacity.is_huge`` signal the task path uses:

  reviewer       base 5 × 2.5 → 13
  test_engineer  base 5 × 1.5 → 8
  domain_expert  base 3 × 1.5 → 5
  critic (absent from the dict) → unchanged

No-op when capacity is None / not huge / role absent / mult ≤ 1.0 / cfg lacks
``task_overrides``. Idempotent — always recomputes from ``spec.max_turns``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from adapters.types import AgentInvocation, AgentResult, AgentSpec
from orchestrator.delegation_envelope import DelegationEnvelope
from orchestrator.execute_phase import delegate
from runtime.repo_probe import RepoCapacity


# Per-role spec base ``max_turns`` so the scaling math matches the plan's
# documented outcomes (reviewer/test_engineer base 5, domain_expert base 3).
_ROLE_BASE_MAX_TURNS: dict[str, int] = {
    "reviewer": 5,
    "test_engineer": 5,
    "domain_expert": 3,
    "critic": 5,
}


def _build_orch_stub(
    tmp_path: Path,
    *,
    capacity: RepoCapacity | None,
    captured: dict,
    with_task_overrides: bool = True,
    ledger_ops: list | None = None,
) -> object:
    """Construct an Orchestrator-shaped stub for the non-task delegate path."""

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
                prompt=f"you are a {role}",
                description="",
                tools=[],
                max_turns=_ROLE_BASE_MAX_TURNS.get(role, 5),
            )

    class FakeGuardrails:
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

        async def ledger_append(self, *, op, payload):
            if ledger_ops is not None:
                ledger_ops.append((op, payload))

    # The huge-repo multiplier dict mirrors schema.py's defaults.
    if with_task_overrides:
        task_overrides = type(
            "TaskOverridesStub",
            (),
            {
                "huge_repo_multipliers": {
                    "reviewer": 2.5,
                    "test_engineer": 1.5,
                    "domain_expert": 1.5,
                },
            },
        )()
    else:
        task_overrides = None

    cfg = type(
        "CfgStub",
        (),
        {
            "agents": {},
            "user_complexity": "medium",
            "task_overrides": task_overrides,
        },
    )()

    return type(
        "OrchStub",
        (),
        {
            "registry": FakeRegistry(),
            "knowledge": FakeKnowledge(),
            "cfg": cfg,
            "plan_manager": FakePlanManager(),
            "adapter": FakeAdapter(),
            "guardrails": FakeGuardrails(),
            "loop_detector": FakeLoop(),
            "cwd": tmp_path,
            "session_id": "s1",
            "_repo_capacity": capacity,
        },
    )()


def _env() -> DelegationEnvelope:
    return DelegationEnvelope(
        task_id="1.1",
        target_agent="reviewer",
        action="review",
        files=[],
        context={},
    )


_HUGE = RepoCapacity(
    file_count=25_000, total_bytes=10_000_000_000, depth_max=10, is_huge=True
)
_NORMAL = RepoCapacity(
    file_count=1_000, total_bytes=10_000_000, depth_max=5, is_huge=False
)


@pytest.mark.asyncio
async def test_reviewer_huge_scaled_to_13(tmp_path: Path) -> None:
    """reviewer base 5 × 2.5 → round(12.5) → 13."""
    captured: dict = {}
    orch = _build_orch_stub(tmp_path, capacity=_HUGE, captured=captured)
    await delegate(orch, "reviewer", _env(), task=None)
    assert captured["max_turns"] == 13


@pytest.mark.asyncio
async def test_reviewer_non_huge_unscaled(tmp_path: Path) -> None:
    """``is_huge=False`` → base 5 unchanged."""
    captured: dict = {}
    orch = _build_orch_stub(tmp_path, capacity=_NORMAL, captured=captured)
    await delegate(orch, "reviewer", _env(), task=None)
    assert captured["max_turns"] == 5


@pytest.mark.asyncio
async def test_reviewer_capacity_none_unscaled(tmp_path: Path) -> None:
    """``_repo_capacity=None`` (probe never ran) → base 5 unchanged."""
    captured: dict = {}
    orch = _build_orch_stub(tmp_path, capacity=None, captured=captured)
    await delegate(orch, "reviewer", _env(), task=None)
    assert captured["max_turns"] == 5


@pytest.mark.asyncio
async def test_test_engineer_huge_scaled_to_8(tmp_path: Path) -> None:
    """test_engineer base 5 × 1.5 → round(7.5) → 8."""
    captured: dict = {}
    orch = _build_orch_stub(tmp_path, capacity=_HUGE, captured=captured)
    await delegate(orch, "test_engineer", _env(), task=None)
    assert captured["max_turns"] == 8


@pytest.mark.asyncio
async def test_domain_expert_huge_scaled_to_5(tmp_path: Path) -> None:
    """domain_expert base 3 × 1.5 → round(4.5) → 5 (banker's rounding ok)."""
    captured: dict = {}
    orch = _build_orch_stub(tmp_path, capacity=_HUGE, captured=captured)
    await delegate(orch, "domain_expert", _env(), task=None)
    assert captured["max_turns"] == 5


@pytest.mark.asyncio
async def test_critic_absent_from_dict_unscaled(tmp_path: Path) -> None:
    """A role absent from the multiplier dict (critic) → base unchanged."""
    captured: dict = {}
    orch = _build_orch_stub(tmp_path, capacity=_HUGE, captured=captured)
    await delegate(orch, "critic", _env(), task=None)
    assert captured["max_turns"] == 5


@pytest.mark.asyncio
async def test_no_task_overrides_is_noop(tmp_path: Path) -> None:
    """cfg without ``task_overrides`` → base unchanged even when huge."""
    captured: dict = {}
    orch = _build_orch_stub(
        tmp_path,
        capacity=_HUGE,
        captured=captured,
        with_task_overrides=False,
    )
    await delegate(orch, "reviewer", _env(), task=None)
    assert captured["max_turns"] == 5


@pytest.mark.asyncio
async def test_scaling_is_idempotent(tmp_path: Path) -> None:
    """Calling twice yields the same scaled value (recompute from base)."""
    captured: dict = {}
    orch = _build_orch_stub(tmp_path, capacity=_HUGE, captured=captured)
    await delegate(orch, "reviewer", _env(), task=None)
    first = captured["max_turns"]
    await delegate(orch, "reviewer", _env(), task=None)
    second = captured["max_turns"]
    assert first == second == 13


@pytest.mark.asyncio
async def test_huge_repo_multiplier_op_emitted(tmp_path: Path) -> None:
    """The existing ``huge_repo_multiplier_applied`` op is emitted on scale."""
    captured: dict = {}
    ledger_ops: list = []
    orch = _build_orch_stub(
        tmp_path, capacity=_HUGE, captured=captured, ledger_ops=ledger_ops
    )
    await delegate(orch, "reviewer", _env(), task=None)
    emitted = [p for (op, p) in ledger_ops if op == "huge_repo_multiplier_applied"]
    assert len(emitted) == 1
    payload = emitted[0]
    assert payload["role"] == "reviewer"
    assert payload["base"] == 5
    assert payload["multiplier"] == 2.5
    assert payload["effective"] == 13


@pytest.mark.asyncio
async def test_non_huge_emits_no_op(tmp_path: Path) -> None:
    """No ``huge_repo_multiplier_applied`` op on a small repo (no scaling)."""
    captured: dict = {}
    ledger_ops: list = []
    orch = _build_orch_stub(
        tmp_path, capacity=_NORMAL, captured=captured, ledger_ops=ledger_ops
    )
    await delegate(orch, "reviewer", _env(), task=None)
    assert not [
        p for (op, p) in ledger_ops if op == "huge_repo_multiplier_applied"
    ]
