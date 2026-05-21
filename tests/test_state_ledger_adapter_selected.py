"""v0.38.0 HK10: ``adapter_selected`` ledger op coverage.

The op is fired exactly once per CLI entry (plan / execute / resume /
tournament phase-review) right after :func:`adapters.detect.get_adapter`
returns. It records:

  * the platform chosen (``"claude_code"`` | ``"cursor"``),
  * the selection source (``"preferred"`` / ``"trigger_context"`` /
    ``"env"`` / ``"fitness"`` / ``"fallback"``),
  * whether trigger-context env was detected (independent of source —
    operators can spot the "context present but didn't win" case),
  * the healthcheck outcome (always True at emission time because a
    failed healthcheck raises ``AdapterError`` upstream).

Audit-only — replay treats it as a no-op so old ledgers stay forward-
compatible.
"""

from __future__ import annotations

import typing

import pytest

from state.ledger import LedgerOp, _apply_op
from fixtures.ledger_ops import make_entry, make_minimal_plan


def test_adapter_selected_registered_in_ledger_op_literal() -> None:
    """v0.38.0 HK10: the new op must be in the ``LedgerOp`` Literal so
    Pydantic validates serialized ledger lines that carry it."""
    assert "adapter_selected" in typing.get_args(LedgerOp)


def test_adapter_selected_apply_is_audit_only_noop() -> None:
    """v0.38.0 HK10: the dispatch handler returns the plan unchanged.
    The selected adapter is recreated by ``get_adapter`` at resume
    time — the op is forensics, not state."""
    plan = make_minimal_plan()
    entry = make_entry(
        "adapter_selected",
        {
            "platform": "claude_code",
            "source": "trigger_context",
            "trigger_context_detected": True,
            "healthcheck_ok": True,
        },
    )
    result = _apply_op(plan, entry)
    # Same plan instance returned — no mutation.
    assert result is plan


def test_adapter_selected_apply_with_empty_payload_still_noop() -> None:
    """v0.38.0 HK10: the dispatch is payload-agnostic. Older / future
    payload shapes still no-op cleanly so partial-rollout fleets
    don't crash on replay."""
    plan = make_minimal_plan()
    entry = make_entry("adapter_selected", {})
    result = _apply_op(plan, entry)
    assert result is plan


@pytest.mark.parametrize(
    "source",
    ["preferred", "trigger_context", "env", "fitness", "fallback"],
)
def test_adapter_selected_apply_accepts_all_source_tags(source: str) -> None:
    """v0.38.0 HK10: every source tag classified by
    ``_classify_selection_source`` round-trips through the ledger
    handler without raising."""
    plan = make_minimal_plan()
    entry = make_entry(
        "adapter_selected",
        {
            "platform": "claude_code",
            "source": source,
            "trigger_context_detected": False,
            "healthcheck_ok": True,
        },
    )
    result = _apply_op(plan, entry)
    assert result is plan
