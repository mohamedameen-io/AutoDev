"""WS-1 'should'-severity ladder cleanups for :mod:`orchestrator.blocker_resolver`.

Two engagement-first cleanups, each proven by a test that turns RED if the fix
is reverted (the broken-control is the assertion that the dedupe/guard params are
present AND load-bearing — pre-fix the rung carried an empty ``params`` dict):

  1. WS1-guardrail-double-budget-widen — ``GUARDRAIL_EXCEEDED`` had TWO
     independent budget-widening mechanisms (this resolver's ``escalate_budget``
     rung + the in-loop ``BudgetEscalationTracker``) with no shared cap, so the
     same guardrail budget was widened twice per cycle. The guardrail rung now
     carries ``defer_to_tracker=True`` (cede the actual widening to the single
     tracker) + a stable ``budget_dedupe_key`` (shared cap key) so the budget is
     widened ONCE per cycle.

  2. WS1-soft-blocker-single-cycle-churn — a ``SOFT_BLOCKER`` ``consult_knowledge``
     re-enable reset the retry budget and could immediately re-escalate in the
     SAME cycle (single-cycle churn). The rung now carries
     ``no_immediate_reescalate=True`` + ``min_cycle_gap>=1`` so the re-enable
     respects the cycle budget before re-engaging.

These are pure-function (``deterministic_action``) assertions — no LLM, no
orchestrator dispatch — so they are cheap and deterministic. A separate
``resolver_enabled`` end-to-end smoke confirms the rung survives the chokepoint.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator import blocker_resolver as br
from orchestrator import failure_classes as fc
from state.schemas import BlockerContext


def _ctx(failure_class: str, **kw: object) -> BlockerContext:
    return BlockerContext(failure_class=failure_class, **kw)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# 1. WS1-guardrail-double-budget-widen
# --------------------------------------------------------------------------


def test_guardrail_escalate_budget_rung_carries_dedupe_contract() -> None:
    """The first guardrail rung is ``escalate_budget`` AND it carries the
    shared-cap dedupe contract so the budget is widened once per cycle.

    Broken-control: pre-fix the rung was built with an EMPTY ``params`` dict —
    these two assertions are exactly what turns red if the fix is reverted.
    """
    ctx = _ctx(fc.GUARDRAIL_EXCEEDED, task_id="9.1", failing_role="developer")
    action = br.deterministic_action(ctx)
    assert action is not None
    assert action.action == "escalate_budget"

    # defer_to_tracker: the single BudgetEscalationTracker owns the actual
    # widening, so the resolver does not ALSO widen independently.
    assert action.params.get("defer_to_tracker") is True

    # A stable, non-empty shared cap key both paths can coordinate on.
    key = action.params.get("budget_dedupe_key")
    assert isinstance(key, str) and key, "expected a non-empty budget_dedupe_key"


def test_guardrail_budget_dedupe_key_is_stable_and_scoped() -> None:
    """The dedupe key encodes (task, role, failure_class) and is deterministic.

    Two contexts that differ only in role MUST produce different keys (so an
    independent (task, role) tracker entry is deduped against the matching
    resolver rung), and the same context twice MUST produce the identical key.
    """
    ctx_a = _ctx(fc.GUARDRAIL_EXCEEDED, task_id="9.1", failing_role="developer")
    ctx_b = _ctx(fc.GUARDRAIL_EXCEEDED, task_id="9.1", failing_role="reviewer")

    key_a1 = br.deterministic_action(ctx_a).params["budget_dedupe_key"]  # type: ignore[union-attr]
    key_a2 = br.deterministic_action(ctx_a).params["budget_dedupe_key"]  # type: ignore[union-attr]
    key_b = br.deterministic_action(ctx_b).params["budget_dedupe_key"]  # type: ignore[union-attr]

    assert key_a1 == key_a2  # deterministic
    assert key_a1 != key_b  # role-scoped
    assert "9.1" in key_a1 and "developer" in key_a1
    assert fc.GUARDRAIL_EXCEEDED in key_a1


def test_guardrail_dedupe_key_helper_matches_rung() -> None:
    """The public-ish ``_budget_dedupe_key`` helper is the single source of the
    key embedded in the rung (so the call site can recompute the SAME key)."""
    ctx = _ctx(fc.GUARDRAIL_EXCEEDED, task_id="3.2", failing_role="test_engineer")
    action = br.deterministic_action(ctx)
    assert action is not None
    assert action.params["budget_dedupe_key"] == br._budget_dedupe_key(ctx)


def test_guardrail_second_rung_unchanged_ask_human() -> None:
    """Once escalate_budget is tried, the ladder still terminates at ask_human
    (the dedupe fix must not alter ladder advancement)."""
    ctx = _ctx(
        fc.GUARDRAIL_EXCEEDED,
        task_id="9.1",
        recovery_already_tried=["escalate_budget"],
    )
    action = br.deterministic_action(ctx)
    assert action is not None
    assert action.action == "ask_human"


# --------------------------------------------------------------------------
# 2. WS1-soft-blocker-single-cycle-churn
# --------------------------------------------------------------------------


def test_soft_blocker_consult_rung_carries_no_immediate_reescalate_guard() -> None:
    """The first soft_blocker rung is ``consult_knowledge`` AND it carries the
    no-immediate-re-escalation guard so the re-enable can't churn in one cycle.

    Broken-control: pre-fix the rung had an EMPTY ``params`` dict — these
    assertions turn red if the guard is reverted.
    """
    ctx = _ctx(fc.SOFT_BLOCKER, task_id="11.1")
    action = br.deterministic_action(ctx)
    assert action is not None
    assert action.action == "consult_knowledge"

    assert action.params.get("no_immediate_reescalate") is True

    gap = action.params.get("min_cycle_gap")
    assert isinstance(gap, int) and gap >= 1, "expected min_cycle_gap >= 1"


def test_soft_blocker_second_rung_unchanged_ask_human() -> None:
    """After consult_knowledge, the soft_blocker ladder still terminates at
    ask_human (the churn guard must not alter ladder advancement)."""
    ctx = _ctx(
        fc.SOFT_BLOCKER,
        task_id="11.1",
        recovery_already_tried=["consult_knowledge"],
    )
    action = br.deterministic_action(ctx)
    assert action is not None
    assert action.action == "ask_human"


def test_other_consult_knowledge_rungs_do_not_get_soft_blocker_guard() -> None:
    """The churn guard is soft_blocker-specific: the test_diagnosis /
    review_escalated consult_knowledge rungs must NOT carry it (they are not the
    immediate-re-enable churn path), so the guard is targeted, not blanket."""
    for cls in (
        fc.TEST_DIAGNOSIS_HARDFAIL,
        fc.TEST_DIAGNOSIS_NO_SIGNAL,
        fc.REVIEW_ESCALATED,
    ):
        action = br.deterministic_action(_ctx(cls, task_id="x.1"))
        assert action is not None
        assert action.action == "consult_knowledge"
        assert "no_immediate_reescalate" not in action.params
        assert "min_cycle_gap" not in action.params


# --------------------------------------------------------------------------
# End-to-end: the contract survives the resolver chokepoint (resolver_enabled).
# --------------------------------------------------------------------------


@pytest.mark.resolver_enabled
@pytest.mark.asyncio
async def test_guardrail_dedupe_contract_survives_resolve_blocker(
    tmp_path: Path,
) -> None:
    """``resolve_blocker`` returns the guardrail rung with the dedupe contract
    intact (the fast-path passes the rung through untouched) and records it."""
    from agents import build_registry
    from config.defaults import default_config
    from orchestrator import Orchestrator
    from state import ledger as ledger_mod

    from stub_adapter import StubAdapter

    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    adapter = StubAdapter({})
    orch = Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=adapter,
        registry=build_registry(cfg),
        session_id="sess-ws1-guardrail",
    )

    ctx = _ctx(fc.GUARDRAIL_EXCEEDED, task_id="9.1", failing_role="developer")
    action = await br.resolve_blocker(orch, ctx)

    assert action.action == "escalate_budget"
    assert action.params.get("defer_to_tracker") is True
    assert action.params.get("budget_dedupe_key") == br._budget_dedupe_key(ctx)
    assert adapter.count("resolver") == 0  # fast-path, no LLM

    # The chosen action's params are persisted (so a resume / call site can read
    # the dedupe key off the ledger).
    chosen = [
        e.payload
        for e in ledger_mod.read_entries(tmp_path)
        if e.op == "resolution_chosen"
    ]
    assert chosen and chosen[0]["action"] == "escalate_budget"
    assert chosen[0]["params"].get("defer_to_tracker") is True


@pytest.mark.resolver_enabled
@pytest.mark.asyncio
async def test_soft_blocker_churn_guard_survives_resolve_blocker(
    tmp_path: Path,
) -> None:
    """``resolve_blocker`` returns the soft_blocker consult_knowledge rung with
    the churn guard intact."""
    from agents import build_registry
    from config.defaults import default_config
    from orchestrator import Orchestrator

    from stub_adapter import StubAdapter

    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    adapter = StubAdapter({})
    orch = Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=adapter,
        registry=build_registry(cfg),
        session_id="sess-ws1-softblock",
    )

    ctx = _ctx(fc.SOFT_BLOCKER, task_id="11.1")
    action = await br.resolve_blocker(orch, ctx)

    assert action.action == "consult_knowledge"
    assert action.params.get("no_immediate_reescalate") is True
    assert action.params.get("min_cycle_gap", 0) >= 1
    assert adapter.count("resolver") == 0
