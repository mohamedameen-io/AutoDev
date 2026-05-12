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

import pytest

from errors import LedgerCorruptError
from state.ledger import LedgerOp, _apply_op

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
