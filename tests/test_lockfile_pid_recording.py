"""v0.23.0 C3 regression: lockfile records holder PID + timestamp.

D-6 finding from the 2026-05-09 Unity stall: ``.autodev/.lock`` was
0 bytes after the orchestrator died, leaving operators no way to tell
whether the lock was actually held vs. abandoned. Now the lockfile
contains ``<pid> <iso8601>`` so a future ``autodev resume`` can report
"PID X started at Y, still alive" or auto-clear a dead-PID stale lock.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from state.lockfile import (
    _is_pid_alive,
    _read_lock_holder,
    plan_lock,
)
from state.paths import lock_path


@pytest.mark.asyncio
async def test_lockfile_records_pid(tmp_path: Path) -> None:
    """After plan_lock acquires, the lockfile contents include the holder PID."""
    async with plan_lock(tmp_path):
        pid, ts = _read_lock_holder(lock_path(tmp_path))
    assert pid == os.getpid()
    assert ts is not None
    assert "T" in ts  # ISO format


@pytest.mark.asyncio
async def test_read_lock_holder_handles_empty_file(tmp_path: Path) -> None:
    """Legacy 0-byte lockfile (pre-C3) returns (None, None)."""
    lf = lock_path(tmp_path)
    lf.parent.mkdir(parents=True, exist_ok=True)
    lf.write_text("")
    pid, ts = _read_lock_holder(lf)
    assert pid is None
    assert ts is None


@pytest.mark.asyncio
async def test_read_lock_holder_handles_malformed_file(tmp_path: Path) -> None:
    lf = lock_path(tmp_path)
    lf.parent.mkdir(parents=True, exist_ok=True)
    lf.write_text("not-a-pid garbage\n")
    pid, ts = _read_lock_holder(lf)
    assert pid is None
    assert ts is None


def test_is_pid_alive_for_self() -> None:
    """The current process is always alive."""
    assert _is_pid_alive(os.getpid()) is True


def test_is_pid_alive_for_dead_pid() -> None:
    """A PID well outside any reasonable range is dead."""
    # 4194304 is way past any realistic PID; the OS reports ProcessLookupError.
    assert _is_pid_alive(4_194_304) is False


@pytest.mark.asyncio
async def test_lockfile_overwrites_stale_pid_on_acquire(tmp_path: Path) -> None:
    """A stale (dead-PID) lockfile is silently overwritten on next acquire."""
    lf = lock_path(tmp_path)
    lf.parent.mkdir(parents=True, exist_ok=True)
    # Plant a dead PID first.
    lf.write_text("4194304 1970-01-01T00:00:00\n")
    async with plan_lock(tmp_path):
        pid, _ = _read_lock_holder(lf)
    # Lock acquire wrote our real PID over the dead one.
    assert pid == os.getpid()
