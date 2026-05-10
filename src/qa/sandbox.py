"""v0.24.0 D2: per-gate sandboxing for QA gates.

The asyncio watchdog from v0.22.1 A1 wraps regex calls in a thread
timeout. That works for cooperative cancellation but a CPU-bound
worker thread cannot be killed mid-flight in CPython — it just keeps
running until it returns. For pathological inputs (catastrophic
regex backtracking, fork-bombing test runners), we want a HARDER
isolation boundary: a separate OS process the orchestrator can SIGKILL.

This module ships the scaffolding: :func:`run_sandboxed` accepts a
top-level callable + args and runs it in a fresh
:class:`multiprocessing.Process`. On wall-clock timeout the worker is
``proc.terminate()``-ed (SIGTERM); if it doesn't exit within 1 second
we follow up with ``proc.kill()`` (SIGKILL). The ``on_timeout``
callback is invoked to produce a synthetic return value so the
orchestrator can continue without the runaway gate blocking it.

This is opt-in for v0.24.0 — call sites flip on per-gate basis. The
existing asyncio watchdog (v0.22.1 A1) remains the first line of
defense; the sandbox is a belt-and-suspenders for gates known to be
CPU-bound.
"""

from __future__ import annotations

import concurrent.futures
import logging
import multiprocessing as mp
from collections.abc import Callable
from typing import Any, TypeVar


_log = logging.getLogger(__name__)


T = TypeVar("T")


_DEFAULT_TIMEOUT_S: float = 30.0
_MP_CTX = mp.get_context("forkserver") if hasattr(mp, "get_context") else None


def _worker_target(
    pipe: Any, func: Callable[..., Any], args: tuple, kwargs: dict
) -> None:
    """Run *func* in the child process and ship the outcome back via *pipe*."""
    try:
        result = func(*args, **kwargs)
        pipe.send(("ok", result))
    except BaseException as exc:  # noqa: BLE001 — re-raised in parent
        pipe.send(("err", exc))
    finally:
        pipe.close()


def run_sandboxed(
    func: Callable[..., T],
    *args: Any,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    on_timeout: Callable[[], T] | None = None,
    **kwargs: Any,
) -> T:
    """Run *func(args, kwargs)* in a fresh worker process with a hard timeout.

    Spawns a one-shot :class:`multiprocessing.Process` (forkserver when
    available, else default ctx). On timeout the worker is
    ``terminate()``-ed and ``on_timeout()`` (if supplied) constructs
    the fallback return value. When ``on_timeout`` is None,
    :class:`concurrent.futures.TimeoutError` propagates.

    Limitations:
        * *func* and its args / return value must be picklable.
        * On platforms without forkserver (Windows), the default
          context is used (typically ``spawn``).
    """
    ctx = _MP_CTX if _MP_CTX is not None else mp
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(
        target=_worker_target,
        args=(child_conn, func, args, kwargs),
        daemon=True,
    )
    proc.start()
    try:
        if parent_conn.poll(timeout_s):
            kind, payload = parent_conn.recv()
            proc.join(timeout=1.0)
            if kind == "ok":
                return payload  # type: ignore[no-any-return]
            raise payload
        # Timeout fired: kill the worker.
        _log.warning(
            "qa.sandbox.timeout func=%s timeout_s=%s",
            getattr(func, "__qualname__", str(func)),
            timeout_s,
        )
        proc.terminate()
        proc.join(timeout=1.0)
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=1.0)
        if on_timeout is not None:
            return on_timeout()
        raise concurrent.futures.TimeoutError(
            f"sandboxed {getattr(func, '__qualname__', func)} exceeded "
            f"{timeout_s}s"
        )
    finally:
        try:
            parent_conn.close()
        except OSError:
            pass
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=0.5)


__all__ = ["run_sandboxed"]
