"""Factory helpers for valid ledger entries + a minimal Plan.

Used by :mod:`tests.test_state_ledger_exhaustive_apply_op` to construct
one ``LedgerEntry`` per ``LedgerOp`` literal value and feed it through
``_apply_op``. The shapes here mirror those produced by the runtime so
the exhaustiveness check is exercising real dispatch paths, not a
caricature.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from state.ledger import LedgerEntry
from state.schemas import (
    AcceptanceCriterion,
    Phase,
    Plan,
    Task,
)


def _iso_now() -> str:
    return _dt.datetime(2026, 5, 12, 0, 0, 0, tzinfo=_dt.timezone.utc).isoformat()


def make_minimal_plan() -> Plan:
    """Return a Plan with one phase + one task — enough for every op."""
    task = Task(
        id="1.1",
        phase_id="1",
        title="example",
        description="example",
        files=["src/math/__init__.py"],
        acceptance=[
            AcceptanceCriterion(id="ac-1", description="works"),
        ],
        assigned_agent="developer",
    )
    phase = Phase(id="1", title="example", description="", tasks=[task])
    now = _iso_now()
    return Plan(
        plan_id="plan-test",
        spec_hash="abc123",
        phases=[phase],
        metadata={"title": "Example Plan"},
        complexity=None,
        edit_scope=["src/math"],
        created_at=now,
        updated_at=now,
    )


def make_entry(op: str, payload: dict[str, Any], seq: int = 1) -> LedgerEntry:
    """Build a ``LedgerEntry`` with a placeholder hash chain.

    ``_apply_op`` reads only ``op`` + ``payload``; the chain fields here
    are filler so :class:`LedgerEntry` validates.
    """
    return LedgerEntry(
        seq=seq,
        timestamp=_iso_now(),
        session_id="test-session",
        op=op,  # type: ignore[arg-type]
        payload=payload,
        prev_hash="",
        self_hash="0" * 16,
    )


def payload_for_op(op: str, plan: Plan) -> dict[str, Any]:
    """Return a payload that lets ``_apply_op`` reach a real branch.

    Goal: the dispatch lookup must NOT fall through to the
    ``unknown op`` raise. If a branch then raises
    :class:`errors.LedgerCorruptError` for a different reason (missing
    field, unknown task id), that is acceptable — it proves the branch
    was found. The exhaustiveness test only fails on "unknown op".
    """
    plan_payload = plan.model_dump(mode="json")
    task_id = plan.phases[0].tasks[0].id
    phase_id = plan.phases[0].id

    payloads: dict[str, dict[str, Any]] = {
        # Plan-payload ops.
        "init_plan": {"plan": plan_payload},
        "update_plan": {"plan": plan_payload},
        "snapshot": {"plan": plan_payload},
        # Task-status mutations.
        "update_task_status": {"task_id": task_id, "status": "complete"},
        "mark_blocked": {"task_id": task_id, "reason": "test"},
        "mark_complete": {"task_id": task_id},
        "append_evidence": {"task_id": task_id, "path": "ev.json"},
        # Corrective sub-tasks.
        "append_corrective_tasks": {
            "phase_id": phase_id,
            "tasks": [],
            "review_status": "accepted",
        },
        "update_phase_meta": {
            "phase_id": phase_id,
            "baseline_commit": "abc",
            "review_status": "accepted",
        },
        # Cascade-block.
        "mark_blocked_descendants": {
            "phase_id": phase_id,
            "failed_task_id": task_id,
            "reason": "test",
            "blocked_task_ids": [],
        },
    }
    # All other ops are audit-only with arbitrary payloads — supply empty.
    return payloads.get(op, {})
