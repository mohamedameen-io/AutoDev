"""Tests for :mod:`orchestrator.blocker_resolver` (ADR-0047, Cluster B core).

These exercise the chokepoint decision function ``resolve_blocker`` plus the
pure helpers (``blocker_key``, ``count_prior_cycles``, ``deterministic_action``)
and the LLM resolver fallback (``_llm_resolve``).

The Orchestrator + StubAdapter are built the same way
``tests/test_orchestrator_plan_phase.py`` does. Only the ``resolver`` role is
ever dispatched here (the deterministic fast-path covers the known classes), so
the stub only needs to provide that role for the LLM-path tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator import blocker_resolver as br
from orchestrator import failure_classes as fc
from state import ledger as ledger_mod
from state.schemas import BlockerContext, ResolutionAction

from stub_adapter import StubAdapter, ok


# --------------------------------------------------------------------------
# Orchestrator construction (mirrors test_orchestrator_plan_phase._make_orch)
# --------------------------------------------------------------------------


def _make_orch(cwd: Path, adapter: StubAdapter) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    registry = build_registry(cfg)
    return Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-test-resolver",
    )


def _ctx(failure_class: str, **kw) -> BlockerContext:
    return BlockerContext(failure_class=failure_class, **kw)


def _ops(cwd: Path) -> list[str]:
    return [e.op for e in ledger_mod.read_entries(cwd)]


def _payloads(cwd: Path, op: str) -> list[dict]:
    return [e.payload for e in ledger_mod.read_entries(cwd) if e.op == op]


# --------------------------------------------------------------------------
# blocker_key
# --------------------------------------------------------------------------


def test_blocker_key_stable_with_task_id() -> None:
    ctx = _ctx(fc.GUARDRAIL_EXCEEDED, task_id="2.3")
    assert br.blocker_key(ctx) == "2.3:guardrail_exceeded"


def test_blocker_key_no_task_id_uses_dash() -> None:
    ctx = _ctx(fc.DAG_INVALID)
    assert br.blocker_key(ctx) == "-:dag_invalid"


# --------------------------------------------------------------------------
# deterministic_action — ladder per known class
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure_class,tried,expected_action",
    [
        # guardrail: escalate_budget, then ask_human
        (fc.GUARDRAIL_EXCEEDED, [], "escalate_budget"),
        (fc.GUARDRAIL_EXCEEDED, ["escalate_budget"], "ask_human"),
        # test_diagnosis: consult_knowledge -> retry_with_changes -> ask_human
        (fc.TEST_DIAGNOSIS_HARDFAIL, [], "consult_knowledge"),
        (fc.TEST_DIAGNOSIS_HARDFAIL, ["consult_knowledge"], "retry_with_changes"),
        (
            fc.TEST_DIAGNOSIS_HARDFAIL,
            ["consult_knowledge", "retry_with_changes"],
            "ask_human",
        ),
        (fc.TEST_DIAGNOSIS_NO_SIGNAL, [], "consult_knowledge"),
        (
            fc.TEST_DIAGNOSIS_NO_SIGNAL,
            ["consult_knowledge", "retry_with_changes"],
            "ask_human",
        ),
        # worker_exception: retry_with_changes -> ask_human
        (fc.WORKER_EXCEPTION, [], "retry_with_changes"),
        (fc.WORKER_EXCEPTION, ["retry_with_changes"], "ask_human"),
        # conflict_*: re_architect -> ask_human
        (fc.CONFLICT_3WAY_FAILED, [], "re_architect"),
        (fc.CONFLICT_3WAY_FAILED, ["re_architect"], "ask_human"),
        (fc.CONFLICT_ABANDON, [], "re_architect"),
        (fc.CONFLICT_REWRITE_CAP_EXCEEDED, [], "re_architect"),
        # worktree_apply_failed: repair_environment -> ask_human
        (fc.WORKTREE_APPLY_FAILED, [], "repair_environment"),
        (fc.WORKTREE_APPLY_FAILED, ["repair_environment"], "ask_human"),
        # phase_degraded: repair_environment -> ask_human (the DOA conversion)
        (fc.PHASE_DEGRADED, [], "repair_environment"),
        (fc.PHASE_DEGRADED, ["repair_environment"], "ask_human"),
        # soft_blocker: consult_knowledge -> ask_human
        (fc.SOFT_BLOCKER, [], "consult_knowledge"),
        (fc.SOFT_BLOCKER, ["consult_knowledge"], "ask_human"),
        # dag_invalid / cross_phase: re_plan -> ask_human
        (fc.DAG_INVALID, [], "re_plan"),
        (fc.DAG_INVALID, ["re_plan"], "ask_human"),
        (fc.CROSS_PHASE_DAG_INVALID, [], "re_plan"),
        (fc.CROSS_PHASE_DAG_INVALID, ["re_plan"], "ask_human"),
        # edit_scope_violation: narrow_scope -> re_plan -> ask_human
        (fc.EDIT_SCOPE_VIOLATION, [], "narrow_scope"),
        (fc.EDIT_SCOPE_VIOLATION, ["narrow_scope"], "re_plan"),
        (
            fc.EDIT_SCOPE_VIOLATION,
            ["narrow_scope", "re_plan"],
            "ask_human",
        ),
    ],
)
def test_deterministic_action_ladder(
    failure_class: str, tried: list[str], expected_action: str
) -> None:
    ctx = _ctx(failure_class, recovery_already_tried=tried, task_id="1.1")
    action = br.deterministic_action(ctx)
    assert action is not None, f"{failure_class} with tried={tried} returned None"
    assert action.action == expected_action
    assert action.rationale, "every deterministic action carries a rationale"


def test_deterministic_action_infra_circuit_falls_through() -> None:
    """infra_circuit_open -> fall_through (legacy quarantine is intentional)."""
    ctx = _ctx(fc.INFRA_CIRCUIT_OPEN, task_id="1.1")
    action = br.deterministic_action(ctx)
    assert action is not None
    assert action.action == "fall_through"


def test_deterministic_action_unknown_returns_none() -> None:
    """A novel class is not in the ladder map -> defer to the LLM (None)."""
    ctx = _ctx("totally_novel_failure", task_id="1.1")
    assert br.deterministic_action(ctx) is None


# --------------------------------------------------------------------------
# count_prior_cycles — resume-safe per-blocker budget
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_prior_cycles_counts_matching_keys(tmp_path: Path) -> None:
    adapter = StubAdapter({})
    orch = _make_orch(tmp_path, adapter)
    ctx = _ctx(fc.WORKER_EXCEPTION, task_id="3.2")
    key = br.blocker_key(ctx)

    assert br.count_prior_cycles(orch, ctx) == 0

    # Seed two matching resolution_chosen entries + one for a different key.
    await orch.plan_manager.ledger_append("resolution_chosen", {"blocker_key": key})
    await orch.plan_manager.ledger_append(
        "resolution_chosen", {"blocker_key": "other:guardrail_exceeded"}
    )
    await orch.plan_manager.ledger_append("resolution_chosen", {"blocker_key": key})

    assert br.count_prior_cycles(orch, ctx) == 2


@pytest.mark.asyncio
async def test_count_prior_cycles_empty_ledger(tmp_path: Path) -> None:
    adapter = StubAdapter({})
    orch = _make_orch(tmp_path, adapter)
    ctx = _ctx(fc.WORKER_EXCEPTION, task_id="9.9")
    # No ledger file yet — must be 0, never raise.
    assert br.count_prior_cycles(orch, ctx) == 0


# --------------------------------------------------------------------------
# resolve_blocker — loop safety
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_blocker_loop_safety_exhausted_budget(tmp_path: Path) -> None:
    """When prior cycles >= max, returns ask_human without dispatching."""
    adapter = StubAdapter({})
    orch = _make_orch(tmp_path, adapter)
    ctx = _ctx(fc.WORKER_EXCEPTION, task_id="4.1")
    key = br.blocker_key(ctx)
    budget = orch.cfg.resolver.max_cycles_per_blocker

    for _ in range(budget):
        await orch.plan_manager.ledger_append(
            "resolution_chosen", {"blocker_key": key}
        )

    action = await br.resolve_blocker(orch, ctx)
    assert action.action == "ask_human"
    assert "budget" in action.rationale.lower()
    # No LLM dispatch happened.
    assert adapter.count("resolver") == 0
    # blocker_escalated + a final resolution_chosen recorded.
    ops = _ops(tmp_path)
    assert "blocker_escalated" in ops


# --------------------------------------------------------------------------
# resolve_blocker — deterministic fast-path (known class)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_blocker_known_class_uses_fast_path(tmp_path: Path) -> None:
    """A known class with fast_path_only_on_known=True never hits the LLM."""
    adapter = StubAdapter({})
    orch = _make_orch(tmp_path, adapter)
    assert orch.cfg.resolver.fast_path_only_on_known is True

    ctx = _ctx(fc.GUARDRAIL_EXCEEDED, task_id="1.1")
    action = await br.resolve_blocker(orch, ctx)
    assert action.action == "escalate_budget"
    assert adapter.count("resolver") == 0

    # Ledger breadcrumbs: blocker_escalated then resolution_chosen.
    ops = _ops(tmp_path)
    assert ops.count("blocker_escalated") == 1
    assert ops.count("resolution_chosen") == 1
    chosen = _payloads(tmp_path, "resolution_chosen")[0]
    assert chosen["blocker_key"] == br.blocker_key(ctx)
    assert chosen["action"] == "escalate_budget"


@pytest.mark.asyncio
async def test_resolve_blocker_escalated_payload_shape(tmp_path: Path) -> None:
    adapter = StubAdapter({})
    orch = _make_orch(tmp_path, adapter)
    ctx = _ctx(
        fc.WORKER_EXCEPTION,
        task_id="2.2",
        phase_id="2",
        failing_role="developer",
        raw_error="boom\n" * 400,
        recovery_already_tried=["retry_with_changes"],
    )
    await br.resolve_blocker(orch, ctx)
    esc = _payloads(tmp_path, "blocker_escalated")[0]
    assert esc["task_id"] == "2.2"
    assert esc["phase_id"] == "2"
    assert esc["failure_class"] == fc.WORKER_EXCEPTION
    assert esc["failing_role"] == "developer"
    assert esc["blocker_key"] == br.blocker_key(ctx)
    assert esc["recovery_already_tried"] == ["retry_with_changes"]
    # raw_error excerpt is truncated to 500 chars.
    assert len(esc["raw_error_excerpt"]) <= 500


# --------------------------------------------------------------------------
# resolve_blocker — LLM path for novel classes
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_blocker_novel_class_uses_llm(tmp_path: Path) -> None:
    """Unknown class -> _llm_resolve; a valid JSON action is returned + recorded."""
    payload = {
        "action": "reroute",
        "params": {"skip_component": "flaky_linter"},
        "rationale": "the linter component is wedged; route around it",
    }
    adapter = StubAdapter(
        {"resolver": ok("Here is my decision:\n```json\n" + json.dumps(payload) + "\n```")}
    )
    orch = _make_orch(tmp_path, adapter)
    ctx = _ctx("novel_thing", task_id="5.1")

    action = await br.resolve_blocker(orch, ctx)
    assert action.action == "reroute"
    assert action.params == {"skip_component": "flaky_linter"}
    assert adapter.count("resolver") == 1

    ops = _ops(tmp_path)
    assert "resolution_chosen" in ops
    chosen = _payloads(tmp_path, "resolution_chosen")[0]
    assert chosen["action"] == "reroute"


@pytest.mark.asyncio
async def test_resolve_blocker_llm_bare_json(tmp_path: Path) -> None:
    """LLM may return bare JSON with no code fence — still parses."""
    payload = {"action": "ask_human", "params": {"question": "which API?"}, "rationale": "ambiguous"}
    adapter = StubAdapter({"resolver": ok(json.dumps(payload))})
    orch = _make_orch(tmp_path, adapter)
    ctx = _ctx("weird_novel", task_id="6.1")
    action = await br.resolve_blocker(orch, ctx)
    assert action.action == "ask_human"
    assert action.params["question"] == "which API?"


# --------------------------------------------------------------------------
# resolver-self-failure -> ask_human (B5)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_blocker_resolver_raises_returns_ask_human(tmp_path: Path) -> None:
    def _boom(inv):
        raise RuntimeError("resolver adapter exploded")

    adapter = StubAdapter({"resolver": _boom})
    orch = _make_orch(tmp_path, adapter)
    ctx = _ctx("novel_explodes", task_id="7.1")
    action = await br.resolve_blocker(orch, ctx)
    assert action.action == "ask_human"
    # Still recorded a resolution_chosen (the ask_human fallback).
    assert "resolution_chosen" in _ops(tmp_path)


@pytest.mark.asyncio
async def test_resolve_blocker_resolver_garbage_returns_ask_human(tmp_path: Path) -> None:
    adapter = StubAdapter({"resolver": ok("I cannot make a decision, sorry. No JSON here.")})
    orch = _make_orch(tmp_path, adapter)
    ctx = _ctx("novel_garbage", task_id="8.1")
    action = await br.resolve_blocker(orch, ctx)
    assert action.action == "ask_human"


@pytest.mark.asyncio
async def test_resolve_blocker_llm_invalid_action_token_returns_ask_human(
    tmp_path: Path,
) -> None:
    """JSON parses but the action token is not in the vocabulary -> ask_human."""
    payload = {"action": "do_a_barrel_roll", "params": {}, "rationale": "x"}
    adapter = StubAdapter({"resolver": ok(json.dumps(payload))})
    orch = _make_orch(tmp_path, adapter)
    ctx = _ctx("novel_badtoken", task_id="9.2")
    action = await br.resolve_blocker(orch, ctx)
    assert action.action == "ask_human"


# --------------------------------------------------------------------------
# fast_path_only_on_known=False -> LLM consulted even for known classes
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_blocker_fast_path_disabled_consults_llm(tmp_path: Path) -> None:
    payload = {"action": "escalate_model", "params": {}, "rationale": "bigger model"}
    adapter = StubAdapter({"resolver": ok(json.dumps(payload))})
    orch = _make_orch(tmp_path, adapter)
    orch.cfg.resolver.fast_path_only_on_known = False
    ctx = _ctx(fc.WORKER_EXCEPTION, task_id="10.1")
    action = await br.resolve_blocker(orch, ctx)
    assert action.action == "escalate_model"
    assert adapter.count("resolver") == 1


# --------------------------------------------------------------------------
# consult_knowledge helper (thin async wrapper)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consult_knowledge_never_raises(tmp_path: Path) -> None:
    adapter = StubAdapter({})
    orch = _make_orch(tmp_path, adapter)
    # No knowledge entries — should return a short (possibly empty) summary str.
    summary = await br.consult_knowledge(orch, _ctx(fc.SOFT_BLOCKER, task_id="11.1"))
    assert isinstance(summary, str)


@pytest.mark.asyncio
async def test_consult_knowledge_no_task_id(tmp_path: Path) -> None:
    adapter = StubAdapter({})
    orch = _make_orch(tmp_path, adapter)
    summary = await br.consult_knowledge(orch, _ctx(fc.DAG_INVALID))
    assert isinstance(summary, str)


# --------------------------------------------------------------------------
# _llm_resolve dispatches the resolver role with the configured model
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_resolve_uses_configured_model(tmp_path: Path) -> None:
    payload = {"action": "fall_through", "params": {}, "rationale": "decline"}
    captured: list = []

    def _capture(inv):
        captured.append(inv)
        return ok(json.dumps(payload))

    adapter = StubAdapter({"resolver": _capture})
    orch = _make_orch(tmp_path, adapter)
    orch.cfg.resolver.model = "opus"
    ctx = _ctx("novel_model", task_id="12.1")
    action = await br._llm_resolve(orch, ctx)
    assert isinstance(action, ResolutionAction)
    assert action.action == "fall_through"
    assert captured and captured[0].role == "resolver"
    assert captured[0].model == "opus"
