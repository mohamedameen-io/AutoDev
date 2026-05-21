"""Unit tests for the JSONL-walk helpers in :mod:`cli.commands.status`.

The status command must render even on a partially-corrupted ledger
(it's the operator's forensic surface; refusing to render when one line
in the JSONL is bad would hide the rest of the useful state). These
tests pin the best-effort contract: missing file → empty dict;
malformed lines → silently skipped; well-formed lines → counted.

Covers v0.38.0 I2 (HK4) helpers:

  * ``_count_ops_by_name(ledger_pth, op_name) → dict[phase_id, count]``
  * ``_collect_corrective_cap_scope_by_phase(ledger_pth) → dict[phase_id, scope]``
"""

from __future__ import annotations

import json
from pathlib import Path

from cli.commands.status import (
    _collect_corrective_cap_scope_by_phase,
    _count_ops_by_name,
)


# ---------------------------------------------------------------------------
# _count_ops_by_name
# ---------------------------------------------------------------------------


def test_count_ops_by_name_missing_file_returns_empty(tmp_path: Path) -> None:
    """A ledger that doesn't exist on disk yields ``{}`` — the helper
    must never raise on a fresh repo with no ledger written yet."""
    result = _count_ops_by_name(
        tmp_path / "nonexistent.jsonl", "corrective_cap_reached"
    )
    assert result == {}


def test_count_ops_by_name_counts_matching_phase_ids(tmp_path: Path) -> None:
    """Per-phase counts aggregate across multiple matching ops; non-
    matching op names are skipped; ops without a ``phase_id`` payload
    field are also skipped (the dict key must always be a valid phase
    id)."""
    lp = tmp_path / "ledger.jsonl"
    rows = [
        {"op": "corrective_cap_reached", "payload": {"phase_id": "1"}},
        {"op": "corrective_cap_reached", "payload": {"phase_id": "1"}},
        {"op": "corrective_cap_reached", "payload": {"phase_id": "2"}},
        # Wrong op name — must be ignored.
        {"op": "snapshot", "payload": {"phase_id": "3"}},
        # Right op, but no phase_id — must be skipped (no synthetic key).
        {"op": "corrective_cap_reached", "payload": {}},
        # Right op, phase_id wrong type — skipped.
        {"op": "corrective_cap_reached", "payload": {"phase_id": 42}},
    ]
    with lp.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")

    result = _count_ops_by_name(lp, "corrective_cap_reached")

    assert result == {"1": 2, "2": 1}


def test_count_ops_by_name_tolerates_malformed_lines(tmp_path: Path) -> None:
    """Malformed JSONL lines are silently skipped; well-formed lines on
    either side of a bad line still count. Blank lines also skipped."""
    lp = tmp_path / "ledger.jsonl"
    with lp.open("w") as fh:
        fh.write(json.dumps(
            {"op": "corrective_cap_reached", "payload": {"phase_id": "1"}}
        ) + "\n")
        fh.write("not json at all\n")  # malformed
        fh.write("\n")  # blank
        fh.write(json.dumps(
            {"op": "corrective_cap_reached", "payload": {"phase_id": "1"}}
        ) + "\n")
        fh.write("{partial json")  # malformed, no newline

    result = _count_ops_by_name(lp, "corrective_cap_reached")

    assert result == {"1": 2}


# ---------------------------------------------------------------------------
# _collect_corrective_cap_scope_by_phase
# ---------------------------------------------------------------------------


def test_collect_scope_missing_file_returns_empty(tmp_path: Path) -> None:
    """Missing ledger → empty dict (no exception). Same forensic-
    surface contract as :func:`_count_ops_by_name`."""
    result = _collect_corrective_cap_scope_by_phase(tmp_path / "nope.jsonl")
    assert result == {}


def test_collect_scope_last_wins(tmp_path: Path) -> None:
    """Later cap-reached entries override earlier ones (operator sees
    the most recent ceiling that fired). Mix of phase + plan scopes
    across two phases pins the per-phase resolution."""
    lp = tmp_path / "ledger.jsonl"
    rows = [
        {"op": "corrective_cap_reached",
         "payload": {"phase_id": "1", "scope": "phase"}},
        # Later entry for phase 1 with scope=plan overrides.
        {"op": "corrective_cap_reached",
         "payload": {"phase_id": "1", "scope": "plan"}},
        {"op": "corrective_cap_reached",
         "payload": {"phase_id": "2", "scope": "phase"}},
    ]
    with lp.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")

    result = _collect_corrective_cap_scope_by_phase(lp)

    assert result == {"1": "plan", "2": "phase"}


def test_collect_scope_pre_i3_ledger_defaults_to_phase(tmp_path: Path) -> None:
    """Pre-I3 ledgers omit the ``scope`` field on ``corrective_cap_reached``
    payloads. The helper must default missing/unrecognised scopes to
    ``"phase"`` so panels rendering legacy ledgers still produce a
    meaningful label."""
    lp = tmp_path / "ledger.jsonl"
    rows = [
        # No scope key at all (pre-I3 shape).
        {"op": "corrective_cap_reached", "payload": {"phase_id": "1"}},
        # Unrecognised scope string — also defaults to "phase".
        {"op": "corrective_cap_reached",
         "payload": {"phase_id": "2", "scope": "bogus"}},
    ]
    with lp.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")

    result = _collect_corrective_cap_scope_by_phase(lp)

    assert result == {"1": "phase", "2": "phase"}


def test_collect_scope_tolerates_malformed_lines(tmp_path: Path) -> None:
    """Same malformed-line tolerance as the count helper."""
    lp = tmp_path / "ledger.jsonl"
    with lp.open("w") as fh:
        fh.write("{not json\n")
        fh.write(json.dumps(
            {"op": "corrective_cap_reached",
             "payload": {"phase_id": "1", "scope": "plan"}}
        ) + "\n")
        fh.write("\n")

    result = _collect_corrective_cap_scope_by_phase(lp)

    assert result == {"1": "plan"}
