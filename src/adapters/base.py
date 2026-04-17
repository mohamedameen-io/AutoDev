"""Abstract base class for platform adapters."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from pathlib import Path

from adapters.types import AgentInvocation, AgentResult, AgentSpec


class PlatformAdapter(ABC):
    """Uniform subprocess-based contract for every LLM platform.

    Concrete adapters spawn `claude -p` / `cursor agent --print` per
    invocation. Every call is stateless — continuity lives in autodev state
    files, not in the LLM session.
    """

    name: str = "abstract"

    @abstractmethod
    async def init_workspace(self, cwd: Path, agents: list[AgentSpec]) -> None:
        """Render platform-native agent files into `cwd`.

        Phase 2 subclasses stub this as no-ops; Phase 3 implements rendering.
        """

    @abstractmethod
    async def execute(self, inv: AgentInvocation) -> AgentResult:
        """Run a single agent invocation to completion."""

    async def parallel(
        self,
        invs: list[AgentInvocation],
        max_concurrent: int = 3,
    ) -> list[AgentResult]:
        """Run `invs` concurrently, capped at `max_concurrent` in flight.

        Results preserve the order of `invs`. Exceptions propagate
        (return_exceptions=False) — the caller is responsible for catch logic.
        """
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        sem = asyncio.Semaphore(max_concurrent)

        async def _one(inv: AgentInvocation) -> AgentResult:
            async with sem:
                return await self.execute(inv)

        return await asyncio.gather(
            *(_one(i) for i in invs),
            return_exceptions=False,
        )

    @abstractmethod
    async def healthcheck(self) -> tuple[bool, str]:
        """Return (ok, details) describing CLI presence / login status."""
