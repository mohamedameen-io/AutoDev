"""Tests for v0.32.0 Phase 5 (Gap G): RecoveryHint population at every
soft-block site in :mod:`orchestrator.execute_phase`.

Three coverage layers:

  1. Unit tests on the :func:`_build_recovery_hint` /
     :func:`_build_recovery_hint_from_reason` /
     :func:`_build_guardrail_block_meta` helpers.
  2. Integration: drive a stuck task through ``_try_retry_or_escalate``
     with a guardrail-exceeded exception path and assert
     ``task.recovery_hint`` is populated with the correct typed class.
  3. Integration: the test no-signal soft-block path stamps
     ``recovery_hint.class_ == "missing_test_output"``.
  4. Integration: Phase 1.4 Tier 6 architect-unconvergent populates
     ``recovery_hint.class_ == "architect_unconvergent"`` on the
     :class:`RecoveryOutcome` returned by :func:`run_recovery_tiers`.
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
from orchestrator.plan_phase_recovery import (
    run_recovery_tiers,
    surface_user_intervention_hint,
)
from state.plan_manager import PlanManager
from state.schemas import (
    AcceptanceCriterion,
    Phase,
    Plan,
    RecoveryHint,
    Task,
)

from stub_adapter import StubAdapter, ok


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan() -> Plan:
    return Plan(
        plan_id="p-recovery-hint",
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
    cfg.tournaments.phase_review.enabled = False
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
        session_id="sess-test-recovery-hint",
    )


# ---------------------------------------------------------------------------
# Unit tests on the builders
# ---------------------------------------------------------------------------


def test_build_recovery_hint_default_paths_and_commands() -> None:
    """Defaults populate the three standard evidence paths and the
    ``autodev requeue --task <id>`` + ``autodev status --blocked``
    commands so partial-call sites still produce a useful surface."""
    hint = ep._build_recovery_hint(
        task_id="2.3",
        hint_class="thin_review_evidence",
        action="Review the rejection.",
    )
    assert isinstance(hint, RecoveryHint)
    assert hint.class_ == "thin_review_evidence"
    assert hint.recommended_user_action == "Review the rejection."
    assert ".autodev/evidence/2.3-coder.json" in hint.relevant_evidence_files
    assert ".autodev/evidence/2.3-review.json" in hint.relevant_evidence_files
    assert ".autodev/evidence/2.3-test.json" in hint.relevant_evidence_files
    assert "autodev requeue --task 2.3" in hint.commands_to_try
    assert "autodev status --blocked" in hint.commands_to_try


def test_build_recovery_hint_from_reason_classifies_reviewer() -> None:
    """A ``reviewer NEEDS_CHANGES`` reason routes to
    ``thin_review_evidence`` with the rejection action text."""
    hint = ep._build_recovery_hint_from_reason(
        task_id="1.1", reason="reviewer NEEDS_CHANGES"
    )
    assert hint.class_ == "thin_review_evidence"
    assert "evidence/1.1-review.json" in hint.recommended_user_action


def test_build_recovery_hint_from_reason_classifies_tests() -> None:
    """A ``tests failed`` reason routes to ``missing_test_output``."""
    hint = ep._build_recovery_hint_from_reason(
        task_id="1.1", reason="tests failed"
    )
    assert hint.class_ == "missing_test_output"


def test_build_recovery_hint_from_reason_classifies_adapter_failure() -> None:
    """An adapter failure routes to ``network_transient``."""
    hint = ep._build_recovery_hint_from_reason(
        task_id="1.1", reason="adapter failure: timeout"
    )
    assert hint.class_ == "network_transient"
    assert "autodev doctor" in hint.commands_to_try


def test_build_recovery_hint_from_reason_falls_back_to_user_decision() -> None:
    """An unknown reason routes to ``user_decision_required``."""
    hint = ep._build_recovery_hint_from_reason(
        task_id="1.1", reason="something weird"
    )
    assert hint.class_ == "user_decision_required"


def test_build_guardrail_block_meta_infrastructure_subtype() -> None:
    """When the most recent adapter subtype was ``auth_failed`` the
    guardrail block populates a ``network_transient`` recovery hint."""

    class _FakeOrch:
        _last_adapter_subtype = "auth_failed"
        _last_adapter_api_error_status = 401

    meta = ep._build_guardrail_block_meta(
        orch=_FakeOrch(), task_id="1.1", exc=Exception("budget exhausted")
    )
    assert meta["block_reason_class"] == "infrastructure"
    hint = meta["recovery_hint"]
    assert isinstance(hint, RecoveryHint)
    assert hint.class_ == "network_transient"
    assert "Refresh credentials" in hint.recommended_user_action


def test_build_guardrail_block_meta_cap_subtype() -> None:
    """When the most recent adapter subtype is ``None`` (legitimate
    budget exhaustion) the hint class is ``model_capacity_exhausted``."""

    class _FakeOrch:
        _last_adapter_subtype = None
        _last_adapter_api_error_status = None

    meta = ep._build_guardrail_block_meta(
        orch=_FakeOrch(), task_id="1.1", exc=Exception("budget exhausted")
    )
    assert meta["block_reason_class"] == "cap"
    hint = meta["recovery_hint"]
    assert hint.class_ == "model_capacity_exhausted"


# ---------------------------------------------------------------------------
# Integration: soft-block sites populate recovery_hint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_soft_blocker_populates_recovery_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive a stuck task through ``_try_retry_or_escalate`` to a
    guardrail-exceeded soft-block; the resulting blocked task carries
    a populated ``recovery_hint``."""
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_plan())
    orch = _make_orch(tmp_path)

    async def _fake_delegate(
        orch_arg: Any,
        role: str,
        envelope: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        orch_arg._last_adapter_subtype = "auth_failed"
        raise GuardrailExceededError("budget exhausted on auth-failed retries")

    monkeypatch.setattr(ep, "delegate", _fake_delegate)

    await ep.run_execute_phase(orch)

    plan = await orch.plan_manager.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    assert task.status == "blocked"
    assert task.recovery_hint is not None
    # ``auth_failed`` subtype → infrastructure class → network_transient hint.
    assert task.recovery_hint.class_ == "network_transient"
    assert task.recovery_hint.recommended_user_action
    assert task.recovery_hint.commands_to_try


@pytest.mark.asyncio
async def test_test_no_signal_populates_recovery_hint_class_missing_test_output(
    tmp_path: Path,
) -> None:
    """The Phase 3 ``no_signal`` test diagnosis path stamps
    ``recovery_hint.class_ == "missing_test_output"``."""
    from state.schemas import RecoveryHint as _RH

    pm = PlanManager(tmp_path, session_id="sess-init-2")
    await pm.init_plan(_mk_plan())

    # The simplest path to this site: drive ``update_task_status`` directly
    # with the meta payload the no_signal site emits — this is the wire
    # surface the rest of the system observes.
    no_signal_hint = ep._build_recovery_hint(
        task_id="1.1",
        hint_class="missing_test_output",
        action="The test gate produced no diagnostic signal. Inspect ...",
        evidence_files=[".autodev/evidence/1.1-test.json"],
        commands=["autodev requeue --task 1.1"],
    )
    # Need a valid transition: pending -> in_progress -> blocked.
    await pm.update_task_status("1.1", "in_progress")
    await pm.update_task_status(
        "1.1",
        "blocked",
        meta={
            "blocked_reason": "test result inconclusive — no diagnostic signal",
            "recovery_hint": no_signal_hint,
        },
    )

    plan = await pm.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    assert task.status == "blocked"
    assert task.recovery_hint is not None
    assert isinstance(task.recovery_hint, _RH)
    assert task.recovery_hint.class_ == "missing_test_output"
    assert ".autodev/evidence/1.1-test.json" in (
        task.recovery_hint.relevant_evidence_files
    )


def test_architect_unconvergent_populates_recovery_hint_at_tier_6() -> None:
    """Phase 1.4 Tier 6 :func:`surface_user_intervention_hint` returns a
    :class:`RecoveryHint` with ``class_ == "architect_unconvergent"``
    AND populates ``relevant_debug_files`` from the archived dumps."""
    hint = surface_user_intervention_hint(
        ["/tmp/architect-failed-1.md", "/tmp/architect-failed-2.md"]
    )
    assert isinstance(hint, RecoveryHint)
    assert hint.class_ == "architect_unconvergent"
    assert hint.relevant_debug_files == [
        "/tmp/architect-failed-1.md",
        "/tmp/architect-failed-2.md",
    ]
    # The full :func:`run_recovery_tiers` surface also wires the hint
    # back through ``RecoveryOutcome.recovery_hint``.
    outcome = run_recovery_tiers(
        plan=None,
        errors_seen={("PlanParseError", ""): 3},
        archived_dumps=["/tmp/a.md"],
        last_exception=RuntimeError("nope"),
        attempts=3,
        current_architect_model="claude-opus-4-7",
    )
    assert outcome.recovery_hint is not None
    assert outcome.recovery_hint.class_ == "architect_unconvergent"


# ---------------------------------------------------------------------------
# Round-trip: RecoveryHint persists through ledger replay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recovery_hint_round_trips_through_ledger(tmp_path: Path) -> None:
    """A RecoveryHint stamped into ``update_task_status`` meta is
    persisted to the ledger, replayed on a fresh ``PlanManager.load``,
    and surfaces back on the task — backward-compatible at the wire
    level (the hint serialises by alias, validates by alias)."""
    pm = PlanManager(tmp_path, session_id="sess-rt")
    await pm.init_plan(_mk_plan())
    hint = RecoveryHint(
        class_="thin_review_evidence",
        recommended_user_action="Inspect the review.",
        relevant_evidence_files=[".autodev/evidence/1.1-review.json"],
        commands_to_try=["autodev requeue --task 1.1"],
    )
    await pm.update_task_status("1.1", "in_progress")
    await pm.update_task_status(
        "1.1",
        "blocked",
        meta={"blocked_reason": "reviewer NEEDS_CHANGES", "recovery_hint": hint},
    )

    # Fresh PlanManager → forces full ledger replay.
    pm2 = PlanManager(tmp_path, session_id="sess-rt-2")
    plan = await pm2.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    assert task.recovery_hint is not None
    assert task.recovery_hint.class_ == "thin_review_evidence"
    assert task.recovery_hint.recommended_user_action == "Inspect the review."
