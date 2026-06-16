"""v0.42.1 F5 — the resolver ENGAGES end-to-end (the Run-5 inertness regression).

Run-5's gate (e) failed because the Universal Blocker Resolver fired 0× in the
field: terminal block sites bypassed it. These tests assert the *engagement*
guarantee through the real single setter ``blocker_guard.block_task`` against an
on-disk ledger (no resolver mocks; ``StubAdapter`` only for the LLM resolver
role). Together with the F1d AST invariant (``test_block_path_invariant.py`` —
every blocked transition goes through ``block_task``), they prove the universal
property: **every** terminal block is preceded by a resolver escalation, so a
silent dead-end is impossible.

Marked ``resolver_enabled`` so the conftest autouse fixture unsets
``AUTODEV_RESOLVER_DISABLED`` (the suite scopes the resolver OFF by default).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from config.defaults import default_config
from orchestrator import failure_classes as fc
from orchestrator.blocker_guard import block_task
from state import ledger as ledger_mod
from state.plan_manager import PlanManager
from state.schemas import Phase, Plan, Task

from stub_adapter import StubAdapter, ok

pytestmark = pytest.mark.resolver_enabled


def _iso() -> str:
    return "2026-06-16T00:00:00+00:00"


def _t(tid: str) -> Task:
    return Task(id=tid, phase_id="1", title=f"task {tid}", description=f"do {tid}")


def _mk_plan(tasks: list[Task]) -> Plan:
    return Plan(
        plan_id="p-resolver-engagement",
        spec_hash="cafe",
        phases=[Phase(id="1", title="p", tasks=tasks)],
        created_at=_iso(),
        updated_at=_iso(),
    )


class _FakeGuard:
    def start_task(self, *a: Any, **k: Any) -> None: ...
    def end_task(self, *a: Any, **k: Any) -> None: ...
    def pre_invocation(self, *a: Any, **k: Any) -> None: ...
    def post_invocation(self, *a: Any, **k: Any) -> None: ...


def _make_orch(tmp_path: Path, pm: PlanManager, adapter: Any = None) -> Any:
    cfg = default_config()
    return type(
        "OrchStub",
        (),
        {
            "cwd": tmp_path,
            "session_id": "test-resolver-engagement",
            "plan_manager": pm,
            "cfg": cfg,
            "guardrails": _FakeGuard(),
            "adapter": adapter,
            "registry": None,
            "knowledge": None,
            "loop_detector": None,
            "plugin_registry": None,
            "disable_impl_tournament": True,
        },
    )()


async def _orch_with_task(
    tmp_path: Path, adapter: Any = None
) -> tuple[Any, Task]:
    pm = PlanManager(tmp_path, session_id="s-engage")
    await pm.init_plan(_mk_plan([_t("1.1")]))
    orch = _make_orch(tmp_path, pm, adapter=adapter)
    plan = await pm.load()
    assert plan is not None
    return orch, plan.phases[0].tasks[0]


def _ops(cwd: Path) -> list[str]:
    return [e.op for e in ledger_mod.read_entries(cwd)]


def assert_no_silent_dead_ends(cwd: Path) -> None:
    """Gate (e): every committed ``blocked`` transition in the ledger is
    preceded (strictly lower seq) by a ``blocker_escalated`` op — i.e. the
    resolver was consulted before the task dead-ended. A degrade leaves the same
    breadcrumb without a ``blocked`` transition, so this only audits real blocks.
    """
    entries = ledger_mod.read_entries(cwd)
    escalated_seqs = [e.seq for e in entries if e.op == "blocker_escalated"]
    blocked = [
        e
        for e in entries
        if e.op == "update_task_status" and e.payload.get("status") == "blocked"
    ]
    for e in blocked:
        assert any(s < e.seq for s in escalated_seqs), (
            f"silent dead-end: blocked transition at seq {e.seq} "
            f"(task {e.payload.get('task_id')}) has no preceding blocker_escalated op"
        )


# ---------------------------------------------------------------------------
# block_task ENGAGES the resolver on a known (fast-path) failure class
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_block_task_engages_resolver_and_recovers(tmp_path: Path) -> None:
    orch, task = await _orch_with_task(tmp_path)
    out = await block_task(
        orch,
        task,
        failure_class=fc.SOFT_BLOCKER,
        raw_error="no improvement after retries",
        meta={"blocked_reason": "soft-blocker: stuck"},
    )
    # The resolver recovered the blocker → block_task returns a RE-ENABLED task,
    # NOT a blocked one. (If block_task bypassed the resolver — the Run-5 inert
    # failure mode — this would be "blocked".)
    assert out.status != "blocked"
    ops = _ops(tmp_path)
    assert "blocker_escalated" in ops
    assert "resolution_chosen" in ops
    # No task was ever committed to ``blocked``.
    assert not any(
        e.op == "update_task_status" and e.payload.get("status") == "blocked"
        for e in ledger_mod.read_entries(tmp_path)
    )


# ---------------------------------------------------------------------------
# block_task still COMMITS blocked when the resolver declines — but with a
# preceding escalation breadcrumb (no silent dead-end).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_block_task_blocks_with_breadcrumb_when_resolver_declines(
    tmp_path: Path,
) -> None:
    # A novel/unknown class routes to the LLM resolver; stub it to ask_human so
    # _apply_resolution falls through to the legacy block commit.
    adapter = StubAdapter(
        {
            "resolver": ok(
                '{"action": "ask_human", "params": {}, '
                '"rationale": "needs a human decision"}'
            )
        }
    )
    orch, task = await _orch_with_task(tmp_path, adapter=adapter)
    out = await block_task(
        orch,
        task,
        failure_class="totally_novel_unseen_failure",
        raw_error="???",
        meta={"blocked_reason": "novel terminal failure"},
    )
    assert out.status == "blocked"  # resolver declined → committed blocked
    ops = _ops(tmp_path)
    assert "blocker_escalated" in ops  # but the resolver WAS consulted first
    assert "resolution_chosen" in ops
    assert_no_silent_dead_ends(tmp_path)


# ---------------------------------------------------------------------------
# The universal invariant holds across a mixed run (a recovery + a block).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_universal_guarantee_across_mixed_outcomes(tmp_path: Path) -> None:
    # First call: known class → fast-path recovery (re-enabled, no block).
    # Second call: novel class → LLM ask_human → committed blocked w/ breadcrumb.
    adapter = StubAdapter(
        {
            "resolver": ok(
                '{"action": "ask_human", "params": {}, "rationale": "human"}'
            )
        }
    )
    pm = PlanManager(tmp_path, session_id="s-mixed")
    await pm.init_plan(_mk_plan([_t("1.1"), _t("1.2")]))
    orch = _make_orch(tmp_path, pm, adapter=adapter)
    plan = await pm.load()
    assert plan is not None
    t1, t2 = plan.phases[0].tasks

    recovered = await block_task(
        orch, t1, failure_class=fc.GUARDRAIL_EXCEEDED, raw_error="cap", meta={}
    )
    blocked = await block_task(
        orch, t2, failure_class="novel_xyz", raw_error="???",
        meta={"blocked_reason": "novel"},
    )

    assert recovered.status != "blocked"
    assert blocked.status == "blocked"
    # The headline guarantee: no blocked transition is a silent dead-end.
    assert_no_silent_dead_ends(tmp_path)


# ---------------------------------------------------------------------------
# Meta-test: the invariant assertion actually CATCHES a silent dead-end, so a
# green gate means something (guards against a vacuous assertion).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invariant_catches_a_direct_silent_block(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s-silent")
    await pm.init_plan(_mk_plan([_t("1.1")]))
    plan = await pm.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    # Bypass block_task entirely — commit a raw blocked transition with no
    # resolver escalation. The invariant MUST flag it.
    await pm.update_task_status(task.id, "blocked", meta={"blocked_reason": "raw"})
    with pytest.raises(AssertionError, match="silent dead-end"):
        assert_no_silent_dead_ends(tmp_path)
