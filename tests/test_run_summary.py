"""Phase 0 (cost/time telemetry): run-summary persistence + cost recovery.

Covers:
  * ``append_run_summary`` writes one JSONL row with the expected keys/types.
  * a write failure is swallowed (best-effort — never fails the run).
  * ``sum_invocation_cost`` sums only ``invocation_cost`` ops and honours the
    ``after_seq`` run-window watermark.
  * the ``CostRecordingAdapter`` chokepoint emits an ``invocation_cost`` op
    for an invocation that NEVER touches ``GuardrailEnforcer.post_invocation``
    (i.e. the tournament-style direct ``adapter.execute`` path), and the
    summed total reflects it — proving tournament cost is captured.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from adapters.base import PlatformAdapter
from adapters.types import AgentInvocation, AgentResult
from state.run_summary import (
    append_run_summary,
    current_ledger_seq,
    run_summary_path,
    sum_invocation_cost,
)


# --- append_run_summary ----------------------------------------------------


def test_append_run_summary_writes_expected_row(tmp_path: Path) -> None:
    ok = append_run_summary(
        tmp_path, phase="plan", cost_usd=1.2345, elapsed_s=42.7, tasks=3
    )
    assert ok is True

    path = run_summary_path(tmp_path)
    assert path.exists()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1

    row = json.loads(lines[0])
    assert set(row.keys()) == {"phase", "cost_usd", "elapsed_s", "tasks", "ts"}
    assert row["phase"] == "plan"
    assert isinstance(row["cost_usd"], float)
    assert isinstance(row["elapsed_s"], float)
    assert isinstance(row["tasks"], int)
    assert isinstance(row["ts"], str)
    assert row["cost_usd"] == pytest.approx(1.2345)
    assert row["tasks"] == 3


def test_append_run_summary_is_idempotent_one_line_per_call(tmp_path: Path) -> None:
    append_run_summary(tmp_path, phase="plan", cost_usd=1.0, elapsed_s=1.0, tasks=1)
    append_run_summary(tmp_path, phase="execute", cost_usd=2.0, elapsed_s=2.0, tasks=2)
    lines = run_summary_path(tmp_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    phases = [json.loads(l)["phase"] for l in lines]
    assert phases == ["plan", "execute"]


def test_append_run_summary_swallows_write_failure(tmp_path: Path) -> None:
    """A write failure MUST be swallowed (returns False, raises nothing)."""
    # Make ``.autodev`` a *file* so mkdir + open("a") both fail underneath.
    autodev = tmp_path / ".autodev"
    autodev.write_text("not a dir", encoding="utf-8")

    # Must not raise.
    ok = append_run_summary(
        tmp_path, phase="execute", cost_usd=9.9, elapsed_s=1.0, tasks=0
    )
    assert ok is False


# --- sum_invocation_cost / current_ledger_seq ------------------------------


def _write_ledger(cwd: Path, records: list[dict]) -> None:
    lp = cwd / ".autodev" / "plan-ledger.jsonl"
    lp.parent.mkdir(parents=True, exist_ok=True)
    with lp.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def test_sum_invocation_cost_filters_op_and_window(tmp_path: Path) -> None:
    _write_ledger(
        tmp_path,
        [
            {"seq": 1, "op": "init_plan", "payload": {}},
            {"seq": 2, "op": "invocation_cost", "payload": {"cost_usd": 0.10}},
            {"seq": 3, "op": "adapter_failure", "payload": {"cost_usd": 99.0}},
            {"seq": 4, "op": "invocation_cost", "payload": {"cost_usd": 0.25}},
            {"seq": 5, "op": "invocation_cost", "payload": {"cost_usd": 0.05}},
        ],
    )
    # Whole ledger: only the three invocation_cost ops count.
    assert sum_invocation_cost(tmp_path) == pytest.approx(0.40)
    # Run window after seq 3: only seq 4 + 5.
    assert sum_invocation_cost(tmp_path, after_seq=3) == pytest.approx(0.30)
    # Watermark equal to the last seq → zero current-run cost.
    assert sum_invocation_cost(tmp_path, after_seq=5) == pytest.approx(0.0)


def test_sum_invocation_cost_missing_ledger_is_zero(tmp_path: Path) -> None:
    assert sum_invocation_cost(tmp_path) == pytest.approx(0.0)


def test_current_ledger_seq(tmp_path: Path) -> None:
    assert current_ledger_seq(tmp_path) == 0
    _write_ledger(
        tmp_path,
        [
            {"seq": 1, "op": "init_plan", "payload": {}},
            {"seq": 2, "op": "invocation_cost", "payload": {"cost_usd": 0.1}},
        ],
    )
    assert current_ledger_seq(tmp_path) == 2


# --- end-to-end: cost-recording chokepoint feeds the summary ---------------


class _FakeAdapter(PlatformAdapter):
    """Adapter that returns a fixed cost and does NOT touch the enforcer."""

    name = "fake"

    def __init__(self, cost: float) -> None:
        self._cost = cost

    async def init_workspace(self, cwd, agents):  # type: ignore[no-untyped-def]
        return None

    async def execute(self, inv: AgentInvocation) -> AgentResult:
        return AgentResult(success=True, text="ok", duration_s=2.0, cost_usd=self._cost)

    async def healthcheck(self):  # type: ignore[no-untyped-def]
        return (True, "ok")


def test_cost_recorder_chokepoint_feeds_sum(tmp_path: Path) -> None:
    """An invocation that bypasses the enforcer is still summed via the op.

    This is the tournament path in miniature: a direct ``adapter.execute``
    with no ``post_invocation`` call. The ``CostRecordingAdapter`` must emit
    an ``invocation_cost`` op so ``sum_invocation_cost`` picks it up.
    """
    from config.defaults import default_config
    from orchestrator import Orchestrator

    cfg = default_config()
    orch = Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=_FakeAdapter(cost=0.0777),
        registry={},
        session_id="sess-cost-test",
    )

    async def _drive() -> None:
        inv = AgentInvocation(role="judge", prompt="p", cwd=tmp_path)
        # Direct execute, NO orch.guardrails.post_invocation — mirrors the
        # tournament surface that under-counts ``plan_cost_usd``.
        await orch.adapter.execute(inv)
        await orch.adapter.execute(inv)

    asyncio.run(_drive())

    # Two invocations × 0.0777 captured through the chokepoint.
    assert sum_invocation_cost(tmp_path) == pytest.approx(0.1554)
    # The enforcer's in-memory total never saw these (proves the gap the
    # ledger-op approach closes).
    assert orch.guardrails.plan_cost_usd == pytest.approx(0.0)
