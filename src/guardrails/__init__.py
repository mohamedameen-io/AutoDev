"""Per-task guardrails enforcement.

Public API:

- :class:`GuardrailEnforcer` — tracks per-task metrics (duration, tool calls,
  invocation count, diff bytes) and raises :class:`GuardrailExceededError`
  when a configured cap is breached.
- :class:`LoopDetector` — detects agent-output loops (same response hash
  produced across a small window of invocations).

Both are instantiated once per :class:`orchestrator.Orchestrator` and
invoked at the boundaries of each :func:`orchestrator.execute_phase.delegate`
call.
"""

from __future__ import annotations

from guardrails.enforcer import GuardrailEnforcer, TaskMetrics
from guardrails.loop_detector import LoopDetector


__all__ = ["GuardrailEnforcer", "LoopDetector", "TaskMetrics"]
