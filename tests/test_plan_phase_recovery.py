"""Tests for v0.32.0 Phase 1.4 — hard-fail recovery tiers.

Three layers of coverage:

* Unit tests on the four recovery helpers in
  :mod:`orchestrator.plan_phase_recovery`.
* End-to-end: tier 4 (scope degradation) succeeds on the third
  architect failure when the recurring path is droppable; the plan
  phase returns a clean plan instead of hard-failing.
* End-to-end: when every tier exhausts, the orchestrator hard-fails
  with the forensic summary attached to the raised exception.
"""

from __future__ import annotations

import datetime as _dt
import subprocess
from pathlib import Path

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator.plan_phase_recovery import (
    RecoveryHintStub,
    RecoveryOutcome,
    ScopeDegradationResult,
    attempt_scope_degradation,
    build_forensic_summary,
    run_recovery_tiers,
    should_escalate_model,
    surface_user_intervention_hint,
)
from state.schemas import (
    AcceptanceCriterion,
    Phase,
    Plan,
    Task,
)

from stub_adapter import StubAdapter, ok


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _bootstrap_repo(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=str(tmp_path), check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=str(tmp_path), check=True
    )
    (tmp_path / "math.py").write_text("def add(a, b): return a + b\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "real.py").write_text("X = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
    subprocess.run(
        ["git", "commit", "-qm", "init"], cwd=str(tmp_path), check=True
    )


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
        session_id="sess-recovery",
    )


# ---------------------------------------------------------------------------
# Tier 4: scope degradation
# ---------------------------------------------------------------------------


def _mk_plan_with_two_scope_entries() -> Plan:
    """Plan with two top-level scope entries so dropping ONE does not
    silently widen to the whole-repo sentinel."""
    return Plan(
        plan_id="p-recovery",
        spec_hash="0123456789abcdef",
        edit_scope=["src", "notes"],
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
                        files=["src/real.py"],
                        complexity="simple",
                    ),
                ],
                acceptance=[AcceptanceCriterion(id="ph-1", description="ok")],
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
        complexity="simple",
    )


def test_attempt_scope_degradation_drops_top_recurrence() -> None:
    plan = _mk_plan_with_two_scope_entries()
    errors_seen = {
        ("notes", "missing_on_disk"): 3,
        ("src/missing", "missing_on_disk"): 1,
    }
    result = attempt_scope_degradation(plan, errors_seen)
    assert result.did_degrade
    assert result.dropped_scope_entry == "notes"
    assert "notes" not in result.new_plan.edit_scope
    assert "src" in result.new_plan.edit_scope


def test_attempt_scope_degradation_refuses_when_would_widen() -> None:
    """Dropping the only scope entry would silently widen — refuse."""
    plan = Plan(
        plan_id="p",
        spec_hash="0123456789abcdef",
        edit_scope=["notes"],  # single entry
        phases=[
            Phase(
                id="1",
                title="t",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="t",
                        description="d",
                        files=["notes"],
                        complexity="simple",
                    ),
                ],
                acceptance=[AcceptanceCriterion(id="ph-1", description="ok")],
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
        complexity="simple",
    )
    errors_seen = {("notes", "missing_on_disk"): 5}
    result = attempt_scope_degradation(plan, errors_seen)
    assert not result.did_degrade
    assert "widen" in result.reason


def test_attempt_scope_degradation_no_path_failures() -> None:
    """Only parse-error class keys → no scope to degrade."""
    plan = _mk_plan_with_two_scope_entries()
    errors_seen = {("PlanParseError", ""): 3}
    result = attempt_scope_degradation(plan, errors_seen)
    assert not result.did_degrade
    assert result.reason == "no_path_failures"


def test_attempt_scope_degradation_below_threshold() -> None:
    plan = _mk_plan_with_two_scope_entries()
    errors_seen = {("notes", "missing_on_disk"): 1}
    result = attempt_scope_degradation(plan, errors_seen)
    assert not result.did_degrade
    assert result.reason == "below_recurrence_threshold"


# ---------------------------------------------------------------------------
# Tier 5: model escalation
# ---------------------------------------------------------------------------


def test_should_escalate_model_sonnet_returns_true() -> None:
    assert should_escalate_model("claude-sonnet-4-20250514") is True
    assert should_escalate_model("sonnet") is True
    assert should_escalate_model("CLAUDE-SONNET-4") is True


def test_should_escalate_model_opus_returns_false() -> None:
    assert should_escalate_model("claude-opus-4-7") is False
    assert should_escalate_model("opus") is False


def test_should_escalate_model_unknown_returns_false() -> None:
    assert should_escalate_model("haiku") is False
    assert should_escalate_model("") is False
    assert should_escalate_model(None) is False


# ---------------------------------------------------------------------------
# Tier 6: user-intervention hint
# ---------------------------------------------------------------------------


def test_surface_user_intervention_hint_carries_archived_dumps() -> None:
    # v0.32.0 Phase 5 (Gap G): surface_user_intervention_hint now
    # returns the real :class:`state.schemas.RecoveryHint` model. The
    # ``archived_dumps`` payload moves to ``relevant_debug_files`` and
    # ``action`` becomes ``recommended_user_action``.
    from state.schemas import RecoveryHint

    hint = surface_user_intervention_hint(
        ["/tmp/architect-failed-1.md", "/tmp/architect-failed-2.md"]
    )
    assert isinstance(hint, RecoveryHint)
    assert hint.class_ == "architect_unconvergent"
    assert "architect" in hint.recommended_user_action.lower()
    assert hint.relevant_debug_files == [
        "/tmp/architect-failed-1.md",
        "/tmp/architect-failed-2.md",
    ]
    # Backward-compatible alias re-exports the same class.
    assert RecoveryHintStub is RecoveryHint


# ---------------------------------------------------------------------------
# Tier 7: forensic summary
# ---------------------------------------------------------------------------


def test_build_forensic_summary_lists_dumps_and_attempts() -> None:
    summary = build_forensic_summary(
        last_exception=RuntimeError("nope"),
        archived_dumps=["/tmp/a.md", "/tmp/b.md"],
        attempts=3,
    )
    assert "3 attempts" in summary
    assert "RuntimeError" in summary
    assert "nope" in summary
    assert "autodev status" in summary
    # Both dumps referenced (relative-form rendering is best-effort).
    assert "a.md" in summary
    assert "b.md" in summary


# ---------------------------------------------------------------------------
# Integration: run_recovery_tiers wires the helpers together
# ---------------------------------------------------------------------------


def test_run_recovery_tiers_returns_typed_outcome() -> None:
    plan = _mk_plan_with_two_scope_entries()
    errors_seen = {("notes", "missing_on_disk"): 3}
    outcome = run_recovery_tiers(
        plan=plan,
        errors_seen=errors_seen,
        archived_dumps=["/tmp/a.md"],
        last_exception=RuntimeError("synthetic"),
        attempts=3,
        current_architect_model="claude-sonnet-4",
    )
    assert isinstance(outcome, RecoveryOutcome)
    # Tier 4 fired: degraded plan present with `notes` removed.
    assert outcome.degraded_plan is not None
    assert outcome.dropped_scope_entry == "notes"
    # Tier 5 fired: model escalation chose opus.
    assert outcome.escalated_model is not None
    assert "opus" in outcome.escalated_model.lower()
    # Tier 6: hint always populated.
    assert outcome.recovery_hint is not None
    assert outcome.recovery_hint.class_ == "architect_unconvergent"
    # Tier 7: forensic summary always populated.
    assert "3 attempts" in outcome.forensic_summary
    # Meta carries the structured class + action for the CLI surface.
    assert outcome.meta["recovery_hint_class"] == "architect_unconvergent"
    assert "architect" in outcome.meta["recovery_hint_action"].lower()


def test_run_recovery_tiers_skips_tier4_when_no_plan() -> None:
    """Architect never produced a parseable plan → tier 4 cannot fire."""
    outcome = run_recovery_tiers(
        plan=None,
        errors_seen={("PlanParseError", ""): 3},
        archived_dumps=["/tmp/a.md"],
        last_exception=RuntimeError("nope"),
        attempts=3,
        current_architect_model="claude-opus-4-7",
    )
    assert outcome.degraded_plan is None
    # Tier 5 also skipped (already on opus).
    assert outcome.escalated_model is None
    # Tiers 6 and 7 always fire.
    assert outcome.recovery_hint is not None
    assert outcome.forensic_summary != ""


# ---------------------------------------------------------------------------
# End-to-end: orchestrator drives recovery on the architect-retry path
# ---------------------------------------------------------------------------


_PLAN_WITH_TWO_SCOPE_ENTRIES_BAD = """
# Plan: Demo

EDIT_SCOPE:
  - src
  - bogus_dir

## Phase 1: Implement

### Task 1.1: real file
  - Description: real
  - Files: src/real.py
  - Acceptance:
    - [ ] passes
"""


@pytest.mark.asyncio
async def test_scope_degradation_on_recurrent_failure(
    tmp_path: Path,
) -> None:
    """Three architect attempts emit the same plan with a non-existent
    ``bogus_dir`` scope entry. After the third failure the recovery
    path drops the entry and a fourth attempt isn't required: the
    degraded plan validates immediately."""
    _bootstrap_repo(tmp_path)
    adapter = StubAdapter(
        {
            "explorer": ok("found"),
            "domain_expert": ok("ok"),
            "architect": [
                ok(_PLAN_WITH_TWO_SCOPE_ENTRIES_BAD),
                ok(_PLAN_WITH_TWO_SCOPE_ENTRIES_BAD),
                ok(_PLAN_WITH_TWO_SCOPE_ENTRIES_BAD),
            ],
        }
    )
    orch = _make_orch(tmp_path, adapter)
    plan = await orch.plan("Add subtract")
    assert plan is not None
    # The architect was called the standard 3 attempts; recovery
    # tier 4 produced the final plan without an additional architect
    # dispatch.
    assert adapter.count("architect") == 3
    # Degraded plan retains src; bogus_dir was dropped.
    assert "src" in plan.edit_scope
    assert "bogus_dir" not in plan.edit_scope


_BAD_PLAN_SINGLE_SCOPE = """
# Plan: Demo

EDIT_SCOPE:
  - bogus_only

## Phase 1: Implement

### Task 1.1: missing file
  - Description: bogus
  - Files: bogus_only/x.py
  - Acceptance:
    - [ ] never gates
"""


@pytest.mark.asyncio
async def test_hard_fail_after_all_tiers_exhausted(
    tmp_path: Path,
) -> None:
    """When the only scope entry is the bogus one, tier 4's empty-scope
    guard refuses to degrade. Tiers 5-7 run but cannot rescue the
    plan; the orchestrator hard-fails with the forensic summary
    appended to the raised exception."""
    from orchestrator.path_validator import PathValidationError

    _bootstrap_repo(tmp_path)
    adapter = StubAdapter(
        {
            "explorer": ok("found"),
            "domain_expert": ok("ok"),
            "architect": [
                ok(_BAD_PLAN_SINGLE_SCOPE),
                ok(_BAD_PLAN_SINGLE_SCOPE),
                ok(_BAD_PLAN_SINGLE_SCOPE),
                # Tier 5 may dispatch a 4th architect call under the
                # opus override; still returns the same bad plan.
                ok(_BAD_PLAN_SINGLE_SCOPE),
            ],
        }
    )
    orch = _make_orch(tmp_path, adapter)
    with pytest.raises(PathValidationError) as exc_info:
        await orch.plan("Add subtract")
    # The forensic summary was appended to the exception args.
    msg = str(exc_info.value)
    assert "Architect plan phase failed after 3 attempts" in msg
    assert "Archived rejected markdown dumps" in msg


# ---------------------------------------------------------------------------
# v0.36.0 D3: structural-retry model routing.
# ---------------------------------------------------------------------------


def test_structural_retry_routes_to_sonnet() -> None:
    """A missing_on_disk failure on opus routes the architect to sonnet."""
    from orchestrator.plan_phase_recovery import should_change_model_for_class

    assert (
        should_change_model_for_class(
            current_model="claude-opus-4-7",
            error_class="missing_on_disk",
            structural_retry_model="sonnet",
        )
        == "sonnet"
    )


def test_structural_retry_routes_md_deliverable_to_sonnet() -> None:
    from orchestrator.plan_phase_recovery import should_change_model_for_class

    assert (
        should_change_model_for_class(
            current_model="claude-opus-4-7",
            error_class="new_md_deliverable",
            structural_retry_model="sonnet",
        )
        == "sonnet"
    )


def test_non_structural_retry_remains_on_opus() -> None:
    """Reasoning-class failures (e.g. PlanParseError exception name) do
    NOT trigger the model swap — opus stays."""
    from orchestrator.plan_phase_recovery import should_change_model_for_class

    assert (
        should_change_model_for_class(
            current_model="claude-opus-4-7",
            error_class="PlanParseError",
            structural_retry_model="sonnet",
        )
        is None
    )


def test_non_opus_current_model_no_swap() -> None:
    """If the architect is already on sonnet, no swap fires."""
    from orchestrator.plan_phase_recovery import should_change_model_for_class

    assert (
        should_change_model_for_class(
            current_model="claude-sonnet-4-5",
            error_class="missing_on_disk",
            structural_retry_model="sonnet",
        )
        is None
    )
