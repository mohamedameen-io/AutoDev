"""Per-task guardrail enforcement.

The enforcer wraps :func:`orchestrator.execute_phase.delegate` calls
with three cap checks:

1. **Duration cap** (``max_duration_s_per_task``) — how long the orchestrator
   has spent on a task since :meth:`GuardrailEnforcer.start_task` was called.
2. **Invocation cap** (``max_tool_calls_per_task`` reused as an upper bound on
   number of agent round-trips) — a proxy safety net for tasks that hammer
   the adapter.
3. **Tool-call cap** (``max_tool_calls_per_task``) — cumulative tool-call
   count across all invocations for the task.
4. **Diff-size cap** (``max_diff_bytes``) — cumulative size of diffs returned
   by the adapter, in UTF-8 bytes.

On breach, the enforcer raises :class:`errors.GuardrailExceededError`.
The execute-phase loop catches that, marks the task ``blocked`` with a
``guardrail_exceeded`` reason, and propagates the exception so the top-level
orchestrator can render a clean error to the user.

Metrics for missing task ids (``start_task`` not called) are silently ignored
— this keeps plan-phase invocations (which use a synthetic ``"plan"`` task
id) compatible without forcing every caller to bookend with start/end.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from adapters.types import AgentInvocation, AgentResult
from config.schema import GuardrailsConfig
from errors import GuardrailExceededError
from autologging import get_logger


log = get_logger(__name__)


@dataclass
class TaskMetrics:
    """Running per-task counters. ``start_time`` is a ``time.monotonic()``."""

    task_id: str
    start_time: float
    tool_call_count: int = 0
    total_diff_bytes: int = 0
    invocation_count: int = 0

    @property
    def elapsed_s(self) -> float:
        """Wall-clock seconds since :meth:`start_task` was called."""
        return time.monotonic() - self.start_time


class GuardrailEnforcer:
    """Tracks per-task metrics and raises on threshold breach.

    Thread-safety: callers are expected to drive this from a single asyncio
    event loop (same pattern as the rest of the orchestrator). The internal
    state is a plain dict — no locking.

    Lifecycle::

        enf = GuardrailEnforcer(cfg.guardrails)
        enf.start_task("1.1")
        try:
            for ... :
                enf.pre_invocation("1.1", inv)          # may raise
                result = await adapter.execute(inv)
                enf.post_invocation("1.1", result)      # may raise
        finally:
            enf.end_task("1.1")
    """

    def __init__(self, cfg: GuardrailsConfig) -> None:
        self.cfg = cfg
        self._metrics: dict[str, TaskMetrics] = {}

    # --- lifecycle ---------------------------------------------------------

    def start_task(self, task_id: str) -> None:
        """Begin tracking ``task_id``. Idempotent — resets timers on re-entry."""
        self._metrics[task_id] = TaskMetrics(
            task_id=task_id, start_time=time.monotonic()
        )

    def end_task(self, task_id: str) -> None:
        """Drop tracking for ``task_id``. Safe to call even if never started."""
        self._metrics.pop(task_id, None)

    # --- hooks -------------------------------------------------------------

    def pre_invocation(self, task_id: str, inv: AgentInvocation) -> None:
        """Run before each adapter call. Raises if a pre-call cap is breached."""
        m = self._metrics.get(task_id)
        if m is None:
            # Untracked task (e.g. plan-phase without explicit start_task) —
            # treat as lenient. Callers that want enforcement must start_task.
            return

        if m.elapsed_s >= self.cfg.max_duration_s_per_task:
            raise GuardrailExceededError(
                f"task {task_id}: duration cap "
                f"{self.cfg.max_duration_s_per_task}s exceeded "
                f"(elapsed={m.elapsed_s:.1f}s)"
            )

        # Invocation cap is a proxy for "too many round-trips even if each
        # round-trip uses zero tool calls". Use the same cap as tool-calls.
        if m.invocation_count >= self.cfg.max_tool_calls_per_task:
            raise GuardrailExceededError(
                f"task {task_id}: invocation cap "
                f"{self.cfg.max_tool_calls_per_task} exceeded"
            )

    def post_invocation(self, task_id: str, result: AgentResult) -> None:
        """Run after each adapter call. Updates metrics; raises on cap breach."""
        m = self._metrics.get(task_id)
        if m is None:
            return

        m.invocation_count += 1
        m.tool_call_count += len(result.tool_calls)

        if m.tool_call_count > self.cfg.max_tool_calls_per_task:
            raise GuardrailExceededError(
                f"task {task_id}: tool-call cap "
                f"{self.cfg.max_tool_calls_per_task} exceeded "
                f"(hit {m.tool_call_count})"
            )

        if result.diff:
            m.total_diff_bytes += len(result.diff.encode("utf-8"))
            if m.total_diff_bytes > self.cfg.max_diff_bytes:
                raise GuardrailExceededError(
                    f"task {task_id}: diff-size cap "
                    f"{self.cfg.max_diff_bytes} bytes exceeded "
                    f"(hit {m.total_diff_bytes})"
                )

    # --- introspection -----------------------------------------------------

    def metrics_snapshot(self, task_id: str) -> dict[str, Any]:
        """Return a JSON-friendly snapshot of the current metrics.

        Returns an empty dict when ``task_id`` is not being tracked.
        """
        m = self._metrics.get(task_id)
        if m is None:
            return {}
        return {
            "elapsed_s": m.elapsed_s,
            "tool_call_count": m.tool_call_count,
            "total_diff_bytes": m.total_diff_bytes,
            "invocation_count": m.invocation_count,
        }

    def is_tracking(self, task_id: str) -> bool:
        return task_id in self._metrics


__all__ = ["GuardrailEnforcer", "TaskMetrics"]
