"""Phase 0 (cost/time telemetry): per-run cost + wall-time summary.

After each ``autodev plan`` / ``autodev execute`` run the CLI persists one
JSON line to ``<cwd>/.autodev/run-summary.jsonl``::

    {"phase": "plan"|"execute", "cost_usd": float, "elapsed_s": float,
     "tasks": int, "ts": "<iso8601>"}

The cost is recovered by summing the audit-only ``invocation_cost`` ledger
ops (see :mod:`orchestrator.cost_recorder`) emitted during the run — these
capture EVERY invocation, including the tournament judges / developers that
bypass :meth:`guardrails.enforcer.GuardrailEnforcer.post_invocation`, so the
total is authoritative (tournaments included).

To isolate one run's cost from a prior run sharing the same ledger, the
caller records the ledger's high-water ``seq`` at command entry
(:func:`current_ledger_seq`) and passes it to :func:`sum_invocation_cost`
as ``after_seq``.

Every function here is best-effort: I/O failures are logged and swallowed
so a telemetry hiccup can never fail a run. The append is idempotent in the
sense that exactly one line is written per run (one call per command).

Recover the end-to-end total cost across all runs with::

    jq -s 'map(.cost_usd) | add' .autodev/run-summary.jsonl
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

from autologging import get_logger
from state.paths import autodev_root, ledger_path

log = get_logger(__name__)

RUN_SUMMARY_FILE: str = "run-summary.jsonl"


def run_summary_path(cwd: Path) -> Path:
    """Return ``<cwd>/.autodev/run-summary.jsonl``."""
    return autodev_root(cwd) / RUN_SUMMARY_FILE


def current_ledger_seq(cwd: Path) -> int:
    """Return the highest ``seq`` currently in the ledger (0 if none).

    Used as a run-start watermark: cost ops appended after this seq belong
    to the current run. Best-effort — returns 0 on any read error so the
    summary degrades to "whole ledger" rather than crashing.
    """
    lp = ledger_path(cwd)
    if not lp.exists():
        return 0
    max_seq = 0
    try:
        with lp.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    rec = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                seq = rec.get("seq")
                if isinstance(seq, int) and seq > max_seq:
                    max_seq = seq
    except OSError as exc:
        log.warning("run_summary.ledger_seq_read_failed", err=str(exc))
        return 0
    return max_seq


def sum_invocation_cost(cwd: Path, after_seq: int = 0) -> float:
    """Sum ``cost_usd`` over ``invocation_cost`` ledger ops with seq > ``after_seq``.

    Returns 0.0 when the ledger is missing / unreadable (best-effort). When
    ``after_seq`` is 0 the whole ledger is summed (useful for an all-runs
    total / tests).

    Emits a debug log with ``ops_count`` (number of invocation_cost ops
    considered in the window) to aid post-hoc diagnosis when cost is 0.0.
    """
    lp = ledger_path(cwd)
    if not lp.exists():
        log.debug("run_summary.cost_sum_no_ledger", after_seq=after_seq)
        return 0.0
    total = 0.0
    ops_count = 0
    try:
        with lp.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    rec = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if rec.get("op") != "invocation_cost":
                    continue
                seq = rec.get("seq")
                if isinstance(seq, int) and seq <= after_seq:
                    continue
                ops_count += 1
                payload = rec.get("payload") or {}
                try:
                    total += float(payload.get("cost_usd", 0.0) or 0.0)
                except (TypeError, ValueError):
                    continue
    except OSError as exc:
        log.warning("run_summary.cost_sum_read_failed", err=str(exc))
        return 0.0
    log.debug(
        "run_summary.cost_sum",
        after_seq=after_seq,
        ops_count=ops_count,
        total_usd=round(total, 6),
    )
    return total


def append_run_summary(
    cwd: Path,
    *,
    phase: str,
    cost_usd: float,
    elapsed_s: float,
    tasks: int,
) -> bool:
    """Append one run-summary JSON line. Returns True on success.

    Best-effort: any failure is logged and swallowed (returns False) so a
    telemetry write NEVER fails the run. Creates the file (and ``.autodev/``)
    if absent. One line per run — the caller invokes this exactly once.
    """
    try:
        path = run_summary_path(cwd)
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "phase": phase,
            "cost_usd": round(float(cost_usd), 6),
            "elapsed_s": round(float(elapsed_s), 3),
            "tasks": int(tasks),
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        }
        line = json.dumps(row, sort_keys=True) + "\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
        log.info(
            "run_summary.appended",
            phase=phase,
            cost_usd=row["cost_usd"],
            elapsed_s=row["elapsed_s"],
            tasks=row["tasks"],
        )
        return True
    except Exception as exc:  # noqa: BLE001 — telemetry must never fail a run
        log.warning("run_summary.append_failed", phase=phase, err=str(exc))
        return False


__all__ = [
    "RUN_SUMMARY_FILE",
    "run_summary_path",
    "current_ledger_seq",
    "sum_invocation_cost",
    "append_run_summary",
]
