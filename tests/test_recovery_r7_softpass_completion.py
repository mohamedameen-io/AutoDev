"""R7 gate (STABLE-RELEASE-GATE.md): an INFRA soft-pass APPROVED ReviewEvidence
must NOT auto-complete a turn-exhausted task.

Phase 1A, Step 8b. A turn-exhausted task may auto-complete on the
``approved-on-exhaustion`` path ONLY on a GENUINE critic/human APPROVED — never
on an INFRA soft-pass ``ReviewEvidence`` that carries ``verdict="APPROVED"``
solely because the *reviewer* ran out of turns. Such evidence is stamped
``soft_passed=True`` (see :func:`orchestrator.execute_phase` reviewer infra
soft-pass site) so the completion gate
(:func:`orchestrator.execute_phase._maybe_accept_approved_on_exhaustion`) can
tell it apart from a real APPROVED.

Cases:
* R7 GATE   : infra soft-pass APPROVED (``soft_passed=True``) + turn-exhausted
              -> NOT completed (refused, observably).
* CONTROL   : genuine APPROVED (``soft_passed`` None/False) + turn-exhausted
              -> completed (proves the gate rejects only soft-passes).
* BROKEN-CTL: reverting the marker check (ignoring ``soft_passed``) re-reds the
              R7 gate — proven by exercising the same helper with a monkey-
              patched check that drops the ``soft_passed`` guard.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator import execute_phase as ep
from state.evidence import write_evidence
from state.schemas import (
    AcceptanceCriterion,
    CoderEvidence,
    Phase,
    Plan,
    ReviewEvidence,
    Task,
)

from stub_adapter import StubAdapter, fail


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan() -> Plan:
    task = Task(
        id="0.1",
        phase_id="0",
        title="Confirm context-identity type + member inventory",
        description="Investigate; the correct output is an empty diff.",
        files=[],
        produces_diff=False,
        acceptance=[
            AcceptanceCriterion(id="ac-1", description="findings confirmed"),
        ],
    )
    return Plan(
        plan_id="p-r7",
        spec_hash="d",
        phases=[Phase(id="0", title="Research", tasks=[task])],
        created_at=_iso(),
        updated_at=_iso(),
    )


async def _make_orch(cwd: Path, adapter: StubAdapter) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    cfg.tournaments.review_tournament_enabled = False
    cfg.qa_retry_min_interval_s = 0.0
    registry = build_registry(cfg)
    orch = Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-r7",
    )
    await orch.plan_manager.init_plan(_mk_plan())
    return orch


def _escalation_exhausted_fail():
    return fail("budget escalation exhausted",
                subtype="error_max_turns_escalation_exhausted")


async def _seed_genuine_approved(cwd: Path, task_id: str) -> None:
    """A REAL reviewer APPROVED — ``soft_passed`` left at its default (None)."""
    await write_evidence(
        cwd,
        task_id,
        ReviewEvidence(
            task_id=task_id,
            verdict="APPROVED",
            issues=[],
            output_text="VERDICT: APPROVED",
            raw_response="VERDICT: APPROVED",
        ),
    )


async def _seed_infra_softpass_approved(cwd: Path, task_id: str) -> None:
    """An INFRA soft-pass APPROVED — schema-distinct via ``soft_passed=True``.

    Mirrors the reviewer infra soft-pass site in ``execute_phase``: the
    reviewer exhausted its turn budget, so the diff was accepted without a
    genuine verdict and stamped with the soft-pass marker.
    """
    await write_evidence(
        cwd,
        task_id,
        ReviewEvidence(
            task_id=task_id,
            verdict="APPROVED",
            issues=[
                "reviewer_infra_softpass: reviewer exhausted turns "
                "(escalated); developer diff accepted without a "
                "reviewer verdict"
            ],
            output_text="",
            raw_response="",
            soft_passed=True,
            soft_pass_reason="reviewer_infra_softpass",
        ),
    )


async def _seed_coder(cwd: Path, task_id: str, diff: str | None) -> None:
    await write_evidence(
        cwd,
        task_id,
        CoderEvidence(
            task_id=task_id,
            diff=diff,
            files_changed=[],
            output_text="empty-diff research artifact",
            success=True,
        ),
    )


# ---------------------------------------------------------------------------
# R7 GATE: infra soft-pass APPROVED + turn-exhausted -> NOT completed.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r7_infra_softpass_approved_does_not_complete(
    tmp_path: Path,
) -> None:
    """The R7 contract: an infra soft-pass APPROVED (``soft_passed=True``)
    for a turn-exhausted task must NOT auto-complete via the
    approved-on-exhaustion path."""
    await _seed_infra_softpass_approved(tmp_path, "0.1")
    await _seed_coder(tmp_path, "0.1", "")
    adapter = StubAdapter({})
    orch = await _make_orch(tmp_path, adapter)
    await orch.plan_manager.update_task_status("0.1", "in_progress")
    task = await orch.plan_manager.get_task("0.1")
    assert task is not None

    result = await ep._maybe_accept_approved_on_exhaustion(
        orch, task, _escalation_exhausted_fail()
    )

    # Refused: no completion. (HEAD currently RETURNS a complete Task here.)
    assert result is None

    # And nothing transitioned the task to complete.
    after = await orch.plan_manager.get_task("0.1")
    assert after is not None
    assert after.status != "complete"


# ---------------------------------------------------------------------------
# CONTROL (non-vacuity): genuine APPROVED + turn-exhausted -> completed.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r7_genuine_approved_still_completes(tmp_path: Path) -> None:
    """A GENUINE APPROVED (``soft_passed`` None/False) for the same
    turn-exhausted task DOES complete — proving the gate rejects only
    soft-passes, not all completions."""
    await _seed_genuine_approved(tmp_path, "0.1")
    await _seed_coder(tmp_path, "0.1", "")
    adapter = StubAdapter({})
    orch = await _make_orch(tmp_path, adapter)
    await orch.plan_manager.update_task_status("0.1", "in_progress")
    task = await orch.plan_manager.get_task("0.1")
    assert task is not None

    result = await ep._maybe_accept_approved_on_exhaustion(
        orch, task, _escalation_exhausted_fail()
    )

    assert result is not None
    assert result.status == "complete"


@pytest.mark.asyncio
async def test_r7_explicit_soft_passed_false_completes(tmp_path: Path) -> None:
    """``soft_passed=False`` (explicit, not None) is ALSO genuine and
    completes — the gate keys off truthiness, so a real APPROVED that
    happens to carry an explicit ``False`` marker is not penalised."""
    await write_evidence(
        tmp_path,
        "0.1",
        ReviewEvidence(
            task_id="0.1",
            verdict="APPROVED",
            issues=[],
            output_text="VERDICT: APPROVED",
            raw_response="VERDICT: APPROVED",
            soft_passed=False,
        ),
    )
    await _seed_coder(tmp_path, "0.1", "")
    adapter = StubAdapter({})
    orch = await _make_orch(tmp_path, adapter)
    await orch.plan_manager.update_task_status("0.1", "in_progress")
    task = await orch.plan_manager.get_task("0.1")
    assert task is not None

    result = await ep._maybe_accept_approved_on_exhaustion(
        orch, task, _escalation_exhausted_fail()
    )

    assert result is not None
    assert result.status == "complete"


# ---------------------------------------------------------------------------
# BROKEN-CONTROL: dropping the ``soft_passed`` guard re-reds the R7 gate.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r7_broken_control_without_marker_check_completes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proves the marker CHECK is load-bearing: if the gate ignored
    ``soft_passed`` (the pre-fix behaviour), the infra soft-pass APPROVED
    would auto-complete the turn-exhausted task.

    We emulate the pre-fix code by reaching into the helper and confirming
    that, with the marker check disabled, the *same* infra soft-pass evidence
    is treated as a genuine APPROVED. The R7 gate test above proves the real
    code path refuses it; this proves the refusal is not vacuous.
    """
    await _seed_infra_softpass_approved(tmp_path, "0.1")
    await _seed_coder(tmp_path, "0.1", "")
    adapter = StubAdapter({})
    orch = await _make_orch(tmp_path, adapter)
    await orch.plan_manager.update_task_status("0.1", "in_progress")
    task = await orch.plan_manager.get_task("0.1")
    assert task is not None

    # Pre-fix behaviour: strip the ``soft_passed`` marker before the gate
    # reads it, so the infra soft-pass looks schema-identical to a genuine
    # APPROVED. We do this by patching ``read_evidence`` to return the
    # evidence with the marker cleared.
    import state.evidence as _ev

    real_read = _ev.read_evidence

    async def _read_without_marker(cwd, task_id, kind):  # type: ignore[no-untyped-def]
        loaded = await real_read(cwd, task_id, kind)
        if loaded is not None and getattr(loaded, "soft_passed", None):
            # Emulate evidence that never had the marker stamped.
            object.__setattr__(loaded, "soft_passed", None)
            object.__setattr__(loaded, "soft_pass_reason", None)
        return loaded

    monkeypatch.setattr(_ev, "read_evidence", _read_without_marker)

    result = await ep._maybe_accept_approved_on_exhaustion(
        orch, task, _escalation_exhausted_fail()
    )

    # With the marker erased, the gate (correctly) cannot tell it apart from
    # a genuine APPROVED and completes — demonstrating the marker is what
    # makes the R7 refusal possible.
    assert result is not None
    assert result.status == "complete"
