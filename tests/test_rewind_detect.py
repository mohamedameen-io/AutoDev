"""Tests for ``state.rewind.detect_last_stable_phase`` (v0.29.0 Bug 9).

The detector replays the ledger and identifies the last phase whose
``update_phase_meta(review_status="accepted")`` entry was preceded by a
matching ``phase_review_complete`` event with ``accept_phase=True``. A
force-accepted phase (review_status set to "accepted" without a
matching real review event — e.g. the corrective auto-accept path that
fires after a guardrail kill) is NOT considered stable, and the
detector falls back to the most recent genuinely-reviewed phase.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
from pathlib import Path

import pytest

from state.ledger import append_entry
from state.plan_manager import PlanManager
from state.rewind import detect_last_stable_phase
from state.schemas import Phase, Plan, Task


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan(phase_ids: list[str]) -> Plan:
    """Multi-phase plan with one trivial task per phase."""
    phases = [
        Phase(
            id=pid,
            title=f"Phase {pid}",
            tasks=[
                Task(
                    id=f"{pid}.1",
                    phase_id=pid,
                    title=f"task in {pid}",
                    description=f"work item for phase {pid}",
                ),
            ],
        )
        for pid in phase_ids
    ]
    return Plan(
        plan_id=f"p-detect-{'-'.join(phase_ids)}",
        spec_hash="0123456789abcdef",
        phases=phases,
        created_at=_iso(),
        updated_at=_iso(),
    )


async def _seed_init(cwd: Path, plan: Plan) -> None:
    pm = PlanManager(cwd, session_id="sess-detect-init")
    await pm.init_plan(plan)


async def _append_phase_review_complete(
    cwd: Path, phase_id: str, accept_phase: bool
) -> None:
    await append_entry(
        cwd,
        op="phase_review_complete",
        payload={
            "tournament_id": f"tour-{phase_id}",
            "phase_id": phase_id,
            "passes": 1,
            "winner": "A",
            "accept_phase": accept_phase,
            "artifact_dir": f".autodev/tournaments/phase-review-{phase_id}",
        },
        session_id="sess-detect-review",
    )


async def _accept_phase_meta(cwd: Path, phase_id: str) -> None:
    pm = PlanManager(cwd, session_id="sess-detect-accept")
    await pm.update_phase_meta(phase_id, review_status="accepted")


async def _force_accept_phase_meta(cwd: Path, phase_id: str) -> None:
    """update_phase_meta(accepted) WITHOUT a preceding phase_review_complete.

    Mirrors the v0.28.0 corrective-auto-accept-after-guardrail-kill
    pathway that motivated Bug 9 in the first place.
    """
    pm = PlanManager(cwd, session_id="sess-detect-force")
    await pm.update_phase_meta(phase_id, review_status="accepted")


# ---------------------------------------------------------------------------
# Test 1 — happy path: real phase_review_complete + accepted → detector
# returns that phase id.
# ---------------------------------------------------------------------------


def test_detect_returns_phase_with_real_phase_review_complete(
    tmp_path: Path,
) -> None:
    cwd = tmp_path
    plan = _mk_plan(["0"])

    async def _seed() -> None:
        await _seed_init(cwd, plan)
        await _append_phase_review_complete(cwd, "0", accept_phase=True)
        await _accept_phase_meta(cwd, "0")

    asyncio.run(_seed())

    assert detect_last_stable_phase(cwd) == "0"


# ---------------------------------------------------------------------------
# Test 2 — force-accept after guardrail kill is NOT stable; detector
# returns the prior genuinely-reviewed phase.
# ---------------------------------------------------------------------------


def test_detect_skips_force_accepted_phase(tmp_path: Path) -> None:
    """Phase 0 has a real review + accept. Phase 1 has a fake accept
    (no preceding ``phase_review_complete``). Detector returns "0"."""
    cwd = tmp_path
    plan = _mk_plan(["0", "1"])

    async def _seed() -> None:
        await _seed_init(cwd, plan)
        # Phase 0: genuine acceptance.
        await _append_phase_review_complete(cwd, "0", accept_phase=True)
        await _accept_phase_meta(cwd, "0")
        # Phase 1: a task gets blocked, then phase is force-accepted with no
        # matching phase_review_complete event in front. Mirrors the
        # corrective auto-accept-after-guardrail-kill bug.
        pm = PlanManager(cwd, session_id="sess-detect-block")
        await pm.update_task_status(
            "1.1", "blocked", meta={"blocked_reason": "auth_failed: 403"}
        )
        await _force_accept_phase_meta(cwd, "1")

    asyncio.run(_seed())

    assert detect_last_stable_phase(cwd) == "0"


# ---------------------------------------------------------------------------
# Test 3 — empty ledger.
# ---------------------------------------------------------------------------


def test_detect_returns_none_for_empty_ledger(tmp_path: Path) -> None:
    """No ledger on disk → detector returns None (no stable phase)."""
    assert detect_last_stable_phase(tmp_path) is None


# ---------------------------------------------------------------------------
# Test 4 — no phase ever genuinely accepted (only force-accepts).
# ---------------------------------------------------------------------------


def test_detect_returns_none_when_no_phase_was_genuinely_accepted(
    tmp_path: Path,
) -> None:
    cwd = tmp_path
    plan = _mk_plan(["0", "1"])

    async def _seed() -> None:
        await _seed_init(cwd, plan)
        # phase_review_complete fired but with accept_phase=False — i.e. the
        # tournament voted to reject. The phase was nonetheless force-flipped
        # to "accepted" later (mirroring the bug).
        await _append_phase_review_complete(cwd, "0", accept_phase=False)
        await _force_accept_phase_meta(cwd, "0")
        # Phase 1: never reviewed, just force-accepted.
        await _force_accept_phase_meta(cwd, "1")

    asyncio.run(_seed())

    assert detect_last_stable_phase(cwd) is None
