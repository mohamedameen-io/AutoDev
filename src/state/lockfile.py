"""Async-friendly wrapper around :class:`filelock.FileLock`.

Usage::

    async with plan_lock(cwd):
        # critical section — reads/writes to .autodev/* serialized

The blocking ``.acquire()`` / ``.release()`` calls run inside
:func:`asyncio.to_thread` so the event loop stays responsive when another
autodev instance is holding the lock.

The lock file lives at ``.autodev/.lock`` (see :mod:`state.paths`).
If ``.autodev/`` is missing it will be created; the lock file itself is safe
to leave between runs.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import AsyncIterator

from filelock import FileLock, Timeout

from errors import AutodevError
from state.paths import ensure_autodev_dir, lock_path


class PlanLockTimeoutError(AutodevError):
    """Raised when :func:`plan_lock` cannot acquire within ``timeout_s``."""


@contextlib.asynccontextmanager
async def plan_lock(cwd: Path, timeout_s: float = 30.0) -> AsyncIterator[None]:
    """Exclusive lock over ``.autodev/``.

    :param cwd: Repository root (the one containing ``.autodev/``).
    :param timeout_s: Maximum time to wait for the lock, in seconds.
    :raises PlanLockTimeoutError: if the lock cannot be acquired in time.
    """
    ensure_autodev_dir(cwd)
    # thread_local=False so concurrent asyncio tasks (each running its own
    # to_thread worker) compete on the on-disk lock the same way separate
    # processes do. Without this flag `filelock` suppresses the OS call
    # whenever the same thread-local has already acquired, which breaks
    # the in-process concurrency tests.
    lock = FileLock(str(lock_path(cwd)), timeout=timeout_s, thread_local=False)
    try:
        await asyncio.to_thread(lock.acquire)
    except Timeout as exc:
        raise PlanLockTimeoutError(
            f"could not acquire .autodev/.lock within {timeout_s}s"
        ) from exc
    try:
        yield
    finally:
        # `release` is fast and non-blocking in the common case; still run
        # through to_thread to avoid any GIL-weird edge cases.
        await asyncio.to_thread(lock.release)


__all__ = ["PlanLockTimeoutError", "plan_lock"]
