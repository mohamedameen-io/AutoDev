"""In-process fake adapter used by Phase-4 orchestrator tests.

The adapter accepts a mapping of ``role -> AgentResult`` (or role -> callable
that returns an ``AgentResult`` given an ``AgentInvocation``). Each call is
recorded in ``self.calls`` for assertions.

No subprocesses are spawned; tests run entirely in-process.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Union

from adapters.base import PlatformAdapter
from adapters.types import AgentInvocation, AgentResult, AgentSpec


StubHandler = Union[
    AgentResult,
    list[AgentResult],
    Callable[[AgentInvocation], AgentResult],
]


class StubAdapter(PlatformAdapter):
    """Test double for :class:`src.adapters.base.PlatformAdapter`.

    ``responses`` may contain:

      - an ``AgentResult`` to return every time the role is invoked;
      - a list of ``AgentResult`` popped FIFO per call for that role;
      - a callable ``fn(inv) -> AgentResult`` for full control.
    """

    name = "stub"

    def __init__(self, responses: dict[str, StubHandler]) -> None:
        self._responses = dict(responses)
        self.calls: list[AgentInvocation] = []
        self._counters: dict[str, int] = {}

    async def init_workspace(self, cwd: Path, agents: list[AgentSpec]) -> None:
        # No-op for stub.
        return

    async def execute(self, inv: AgentInvocation) -> AgentResult:
        self.calls.append(inv)
        self._counters[inv.role] = self._counters.get(inv.role, 0) + 1
        handler = self._responses.get(inv.role)
        if handler is None:
            return AgentResult(
                success=True,
                text=f"[stub:{inv.role}] default-ok",
                duration_s=0.01,
            )
        if callable(handler):
            return handler(inv)
        if isinstance(handler, list):
            idx = self._counters[inv.role] - 1
            if idx >= len(handler):
                # Reuse the last entry for "and then always return this".
                return handler[-1]
            return handler[idx]
        return handler

    async def healthcheck(self) -> tuple[bool, str]:
        return True, "stub"

    # --- Test helpers ---

    def count(self, role: str) -> int:
        return self._counters.get(role, 0)

    def prompts_for(self, role: str) -> list[str]:
        return [c.prompt for c in self.calls if c.role == role]


def ok(text: str, **kwargs: Any) -> AgentResult:
    """Convenience builder for a successful :class:`AgentResult`."""
    return AgentResult(success=True, text=text, duration_s=0.01, **kwargs)


def fail(error: str, **kwargs: Any) -> AgentResult:
    """Convenience builder for a failed :class:`AgentResult`."""
    return AgentResult(success=False, text="", duration_s=0.01, error=error, **kwargs)
