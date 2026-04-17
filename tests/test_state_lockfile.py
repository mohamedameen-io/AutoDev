"""Tests for :mod:`src.state.lockfile`."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from state.lockfile import PlanLockTimeoutError, plan_lock
from state.paths import autodev_root, lock_path


@pytest.mark.asyncio
async def test_lock_acquired_and_released(tmp_path: Path) -> None:
    async with plan_lock(tmp_path, timeout_s=2.0):
        pass  # acquired
    # re-acquire should succeed (released fully).
    async with plan_lock(tmp_path, timeout_s=2.0):
        pass


@pytest.mark.asyncio
async def test_lock_creates_autodev_dir(tmp_path: Path) -> None:
    async with plan_lock(tmp_path, timeout_s=2.0):
        assert autodev_root(tmp_path).exists()
        assert lock_path(tmp_path).exists()


@pytest.mark.asyncio
async def test_lock_timeout_raises_cleanly(tmp_path: Path) -> None:
    """Holding the lock, another task with a short timeout must raise."""
    released = asyncio.Event()

    async def holder() -> None:
        async with plan_lock(tmp_path, timeout_s=5.0):
            await released.wait()

    holder_task = asyncio.create_task(holder())
    await asyncio.sleep(0.05)  # let holder acquire

    with pytest.raises(PlanLockTimeoutError):
        async with plan_lock(tmp_path, timeout_s=0.2):
            pass

    released.set()
    await holder_task


@pytest.mark.asyncio
async def test_lock_serializes_concurrent_tasks(tmp_path: Path) -> None:
    """Two awaiters must run one-at-a-time inside the critical section."""
    concurrent = 0
    peak = 0

    async def worker() -> None:
        nonlocal concurrent, peak
        async with plan_lock(tmp_path, timeout_s=5.0):
            concurrent += 1
            peak = max(peak, concurrent)
            await asyncio.sleep(0.05)
            concurrent -= 1

    await asyncio.gather(worker(), worker(), worker())
    assert peak == 1
