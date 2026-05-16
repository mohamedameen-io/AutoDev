"""Abstract base class for platform adapters."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from pathlib import Path

from adapters.types import AgentInvocation, AgentResult, AgentSpec


class NetworkProbeFailure(Exception):
    """v0.36.0 F2: structured network-probe failure.

    Adapters MAY raise this from :meth:`PlatformAdapter.healthcheck` to
    signal that the probe retried the configured number of times AND
    still failed. Distinct from the legacy ``(False, "network: ...")``
    return path so callers can opt-in to a structured handler (the CLI
    `autodev plan` catch site renders ``.suggestion`` and exits with
    a dedicated code 5).

    Fields:
        adapter: short name of the failing adapter ("claude_code", …).
        attempts: number of probes attempted before giving up.
        last_error: stringified terminal-attempt exception / status.
        suggestion: free-form remediation hint surfaced to the operator.
    """

    def __init__(
        self,
        adapter: str,
        attempts: int,
        last_error: str,
        suggestion: str = "",
    ) -> None:
        super().__init__(
            f"network probe for adapter {adapter!r} failed after "
            f"{attempts} attempts: {last_error}"
        )
        self.adapter = adapter
        self.attempts = attempts
        self.last_error = last_error
        self.suggestion = suggestion


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
        """Return ``(ok, details)`` describing CLI presence + login status.

        Contract:
          * On success → ``(True, <human-readable status>)`` (e.g. version
            string).
          * On any failure → ``(False, <reason>)``. Concrete adapters MUST
            distinguish failure modes via a stable reason prefix so callers
            (e.g. ``autodev resume`` / ``execute`` preflight) can render
            actionable guidance:
              - ``"binary not found: ..."`` → CLI missing.
              - ``"auth_failed: ..."``       → reachable CLI, bad/expired
                credentials (HTTP 401/403 from the upstream LLM API).
              - ``"network: ..."``           → reachable CLI, transient
                upstream failure (timeout, 5xx, connection error).

        v0.36.0 F2: adapters MAY ALSO raise :class:`NetworkProbeFailure`
        for structured probe failures (after exhausting the configured
        retry budget). Callers that catch this exception get a typed
        ``.suggestion`` field; callers that don't see the legacy
        ``(False, "network: ...")`` tuple (back-compat).

        Implementations should fail fast (a few seconds, not minutes) so the
        check is safe to gate startup on. The Claude Code adapter implements
        a two-stage probe: ``--version`` (cheap) then a live PONG round-trip
        (catches auth/network).
        """
