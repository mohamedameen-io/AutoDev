"""v0.24.0 D1 regression: ``stream_entries`` yields without buffering.

The legacy ``read_entries`` materializes the full ledger into a list.
For multi-MB ledgers (Unity's 2.97 MB / 140 entries was already
non-trivial; production runs may push higher), we want a streaming
iterator that validates incrementally and yields one entry at a time.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from state.ledger import LedgerEntry, append_entry, read_entries, stream_entries
from state.lockfile import plan_lock


def test_stream_entries_returns_iterator() -> None:
    """The function returns a generator (not a list)."""
    g = stream_entries(Path("/tmp/nonexistent"))
    assert inspect.isgenerator(g)


def test_stream_entries_empty_workspace_yields_nothing(tmp_path: Path) -> None:
    """Missing ledger file yields zero entries."""
    assert list(stream_entries(tmp_path)) == []


@pytest.mark.asyncio
async def test_stream_entries_yields_in_order(tmp_path: Path) -> None:
    """Entries come out in seq order matching the on-disk file."""
    async with plan_lock(tmp_path):
        for i in range(5):
            await append_entry(
                tmp_path,
                op="attempt_started",
                payload={
                    "task_id": f"1.{i}",
                    "attempt_n": 0,
                    "started_at": "2026-05-10T00:00:00",
                    "session_id": "t",
                },
                session_id="t",
            )
    seqs = [e.seq for e in stream_entries(tmp_path)]
    assert seqs == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_stream_and_read_agree(tmp_path: Path) -> None:
    """``read_entries`` is now a thin buffered wrapper over ``stream_entries``."""
    async with plan_lock(tmp_path):
        for i in range(3):
            await append_entry(
                tmp_path,
                op="attempt_started",
                payload={
                    "task_id": f"1.{i}",
                    "attempt_n": 0,
                    "started_at": "x",
                    "session_id": "t",
                },
                session_id="t",
            )
    streamed = list(stream_entries(tmp_path))
    buffered = read_entries(tmp_path)
    assert len(streamed) == len(buffered) == 3
    for s, b in zip(streamed, buffered):
        assert isinstance(s, LedgerEntry)
        assert s.seq == b.seq
        assert s.self_hash == b.self_hash


def test_stream_entries_validates_malformed_json(tmp_path: Path) -> None:
    """Stream raises LedgerCorruptError on a non-JSON line."""
    from state.paths import ledger_path
    from state.ledger import LedgerCorruptError

    lp = ledger_path(tmp_path)
    lp.parent.mkdir(parents=True, exist_ok=True)
    lp.write_text("not-valid-json\n", encoding="utf-8")

    with pytest.raises(LedgerCorruptError):
        list(stream_entries(tmp_path))
