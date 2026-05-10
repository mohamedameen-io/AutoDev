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

v0.23.0 C3: the lock file additionally records the holder PID + ISO
timestamp on acquire (was 0 bytes pre-C3). On stale-lock detection the
recorded PID is checked with ``os.kill(pid, 0)``; dead-PID locks are
auto-cleared, alive-PID locks log a structured warning before the
30 s ``filelock`` timeout fires.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import logging
import os
from pathlib import Path
from typing import AsyncIterator

from filelock import FileLock, Timeout

from errors import AutodevError
from state.paths import ensure_autodev_dir, lock_path


_log = logging.getLogger(__name__)


class PlanLockTimeoutError(AutodevError):
    """Raised when :func:`plan_lock` cannot acquire within ``timeout_s``."""


def _read_lock_holder(p: Path) -> tuple[int | None, str | None]:
    """v0.23.0 C3: return ``(pid, iso_ts)`` recorded in the lock file.

    Returns ``(None, None)`` when the file is missing, empty (legacy), or
    malformed. Format is two whitespace-separated tokens: ``<pid> <iso8601>``.
    """
    try:
        text = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None, None
    if not text:
        return None, None
    parts = text.split(maxsplit=1)
    try:
        pid = int(parts[0])
    except (ValueError, IndexError):
        return None, None
    ts = parts[1] if len(parts) > 1 else None
    return pid, ts


def _is_pid_alive(pid: int) -> bool:
    """v0.23.0 C3: ``True`` iff the OS still has a process with this PID."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by another user — still "alive" for
        # the purpose of stale-lock detection.
        return True
    except OSError:
        return False
    return True


@contextlib.asynccontextmanager
async def plan_lock(cwd: Path, timeout_s: float = 30.0) -> AsyncIterator[None]:
    """Exclusive lock over ``.autodev/``.

    :param cwd: Repository root (the one containing ``.autodev/``).
    :param timeout_s: Maximum time to wait for the lock, in seconds.
    :raises PlanLockTimeoutError: if the lock cannot be acquired in time.
    """
    ensure_autodev_dir(cwd)
    lf = lock_path(cwd)

    # v0.23.0 C3: stale-lock pre-check. If a previous process recorded a
    # PID that is no longer alive, the on-disk content is forensics only
    # — the fcntl lock from that process is already gone. Surface a
    # warning so operators see the recovery, then proceed; if the PID is
    # alive, log a different message before the timeout fires.
    if lf.exists():
        prev_pid, prev_ts = _read_lock_holder(lf)
        if prev_pid is not None:
            if _is_pid_alive(prev_pid):
                _log.warning(
                    "lockfile.held_by_active_process pid=%s started_at=%s",
                    prev_pid,
                    prev_ts,
                )
            else:
                _log.warning(
                    "lockfile.stale_pid_cleared pid=%s started_at=%s",
                    prev_pid,
                    prev_ts,
                )
                # Do NOT remove the file (filelock manages the fcntl
                # advisory lock; the file presence is benign). The empty
                # content gets overwritten below on successful acquire.

    # thread_local=False so concurrent asyncio tasks (each running its own
    # to_thread worker) compete on the on-disk lock the same way separate
    # processes do. Without this flag `filelock` suppresses the OS call
    # whenever the same thread-local has already acquired, which breaks
    # the in-process concurrency tests.
    lock = FileLock(str(lf), timeout=timeout_s, thread_local=False)
    try:
        await asyncio.to_thread(lock.acquire)
    except Timeout as exc:
        raise PlanLockTimeoutError(
            f"could not acquire .autodev/.lock within {timeout_s}s"
        ) from exc

    # v0.23.0 C3: record holder PID + start time for diagnostics.
    try:
        ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
        lf.write_text(f"{os.getpid()} {ts}\n", encoding="utf-8")
    except OSError as exc:
        _log.debug("lockfile.holder_record_failed err=%s", str(exc))

    try:
        yield
    finally:
        # `release` is fast and non-blocking in the common case; still run
        # through to_thread to avoid any GIL-weird edge cases.
        await asyncio.to_thread(lock.release)


__all__ = ["PlanLockTimeoutError", "plan_lock"]
