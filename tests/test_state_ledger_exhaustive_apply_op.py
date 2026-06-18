"""Phase 0: every ``LedgerOp`` Literal value must have an ``_apply_op`` branch.

This is the exhaustiveness check that v0.26.1 lacked. When the
``architect_consult`` op landed in the Literal without a handler,
replaying a real session ledger raised ``LedgerCorruptError("unknown op")``
mid-resume — the kind of bug that only surfaces in production.

The test iterates ``typing.get_args(LedgerOp)`` and asserts that the
``_apply_op`` dispatch resolves every value to *some* branch — i.e. it
does NOT fall through to the trailing ``raise LedgerCorruptError(...
unknown op=...)``. A branch that raises a different
``LedgerCorruptError`` (missing payload field, etc.) still counts as
"reached" — the safeguard is specifically against silent omission from
the dispatch table.

When v0.27 phases 4/5/9 add new ops to the Literal, the test fails
until each new op gets a handler.
"""

from __future__ import annotations

import typing
from pathlib import Path

import pytest

from errors import LedgerCorruptError
from state.ledger import LedgerOp, _apply_op, append_entry, replay_ledger

from fixtures.ledger_ops import (
    make_entry,
    make_minimal_plan,
    payload_for_op,
)


def _is_unknown_op_error(exc: LedgerCorruptError) -> bool:
    """True only when the error message matches the fall-through raise."""
    return "unknown op" in str(exc)


@pytest.mark.parametrize("op", typing.get_args(LedgerOp))
def test_every_ledger_op_has_apply_handler(op: str) -> None:
    """For each ``LedgerOp`` literal, dispatch must NOT fall through.

    Acceptable outcomes:
      * ``_apply_op`` returns normally;
      * ``_apply_op`` raises ``LedgerCorruptError`` for a NON-"unknown
        op" reason (e.g. missing payload field) — proves the dispatch
        located its branch.

    Failure: the error message contains ``"unknown op"`` — the op is
    in the Literal but has no handler.
    """
    plan = make_minimal_plan()
    payload = payload_for_op(op, plan)
    entry = make_entry(op, payload)

    try:
        _apply_op(plan, entry)
    except LedgerCorruptError as exc:
        assert not _is_unknown_op_error(exc), (
            f"op {op!r} reached the unknown-op fall-through in _apply_op. "
            "Every value in the LedgerOp Literal must have a dispatch "
            f"branch. (Error: {exc})"
        )
    except Exception:
        # Any other exception is fine — we only police the dispatch table.
        pass


def test_task_under_decomposed_replays_as_noop() -> None:
    """v0.39.0 (Cluster C2d): ``task_under_decomposed`` is audit-only.

    It must round-trip through ``_apply_op`` as a no-op (return the plan
    unchanged), NOT raise ``LedgerCorruptError("unknown op")``. This guards
    the regression where adding the op to the ``LedgerOp`` Literal without
    a matching dispatch branch makes any ledger containing it un-replayable.
    """
    plan = make_minimal_plan()
    entry = make_entry(
        "task_under_decomposed",
        {
            "task_id": "1.1",
            "source": "planner_advisory",
            "attempt": 0,
            "file_count": 8,
            "files": ["a.py", "b.py"],
            "complexity": "complex",
        },
    )
    result = _apply_op(plan, entry)
    assert result is plan


def test_invocation_cost_replays_as_noop() -> None:
    """Phase 0 (cost/time telemetry): ``invocation_cost`` is audit-only.

    It must round-trip through ``_apply_op`` as a no-op (return the plan
    unchanged), NOT raise ``LedgerCorruptError("unknown op")``. Mirrors the
    ``task_under_decomposed`` guard: adding the op to the ``LedgerOp``
    Literal without a matching dispatch branch would make any ledger
    containing it un-replayable.
    """
    plan = make_minimal_plan()
    entry = make_entry(
        "invocation_cost",
        {
            "role": "developer",
            "task_id": "1.1",
            "cost_usd": 0.0123,
            "duration_s": 4.5,
        },
    )
    result = _apply_op(plan, entry)
    assert result is plan


def test_apply_op_unknown_label_still_raises() -> None:
    """Sanity: an op string NOT in the Literal still triggers the
    fall-through raise. Confirms the exhaustiveness check above will
    actually fail when a new op forgets its handler.
    """
    plan = make_minimal_plan()
    # Bypass Pydantic's Literal validation so we can pass an unknown op.
    entry = make_entry("init_plan", {"plan": plan.model_dump(mode="json")})
    # Mutate the validated entry post-construction.
    object.__setattr__(entry, "op", "definitely-not-a-real-op")  # type: ignore[arg-type]

    with pytest.raises(LedgerCorruptError) as exc_info:
        _apply_op(plan, entry)
    assert _is_unknown_op_error(exc_info.value)


@pytest.mark.asyncio
async def test_replay_ledger_survives_advisory_ops(tmp_path: Path) -> None:
    """Regression guard for the B2 class of bug: unregistered op → replay_ledger raises.

    Writes a real chained ledger with:
        1. ``init_plan``  — establishes the plan
        2. ``over_engineering_advisory``  — planner-side advisory (B1)
        3. ``reviewer_over_engineering_advisory``  — reviewer-side advisory (B2)

    Then calls ``replay_ledger(tmp_path)`` and asserts:
        * it does NOT raise
        * it returns a non-None plan
        * all 3 entries are in the returned list

    This test WILL fail if either advisory op is removed from ``_apply_op``
    (the removed op hits the fall-through ``LedgerCorruptError("unknown op=...")``
    and replay_ledger propagates it).
    """
    plan = make_minimal_plan()
    session = "sess-replay-test"

    # 1. init_plan — establishes the plan in the ledger
    await append_entry(
        tmp_path,
        "init_plan",
        {"plan": plan.model_dump(mode="json")},
        session_id=session,
    )

    # 2. over_engineering_advisory (B1: planner-side, fires after plan parse)
    await append_entry(
        tmp_path,
        "over_engineering_advisory",
        {
            "task_id": "1.1",
            "smell": "unnecessary abstraction",
            "source": "planner_advisory",
            "attempt": 0,
        },
        session_id=session,
    )

    # 3. reviewer_over_engineering_advisory (B2: reviewer-side, fires after phase review)
    await append_entry(
        tmp_path,
        "reviewer_over_engineering_advisory",
        {
            "phase_id": "1",
            "note": "Abstraction layer adds no value.",
            "source": "reviewer_advisory",
        },
        session_id=session,
    )

    # replay_ledger must not raise
    result_plan, entries = replay_ledger(tmp_path)

    assert result_plan is not None, "replay_ledger returned None plan — advisory op may be unregistered"
    assert len(entries) == 3, f"Expected 3 entries, got {len(entries)}"
    ops = [e.op for e in entries]
    assert ops == [
        "init_plan",
        "over_engineering_advisory",
        "reviewer_over_engineering_advisory",
    ]
