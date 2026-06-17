"""Abstract base class for platform adapters."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from adapters.types import AgentInvocation, AgentResult, AgentSpec
from autologging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class AdapterCapabilities:
    """Declared, machine-checkable capabilities of a concrete adapter.

    WS3 (stabilization-v1): previously the only signal that an adapter
    could not enforce ``allowed_tools`` was a *per-invocation* warning
    buried inside ``CursorAdapter._build_command``. A caller that needs
    tool-scoping for *correctness* (e.g. the text-only tournament roles
    ``critic_t`` / ``synthesizer``, which MUST run with zero tools so a
    speculative read can't burn their only turn) had no way to discover
    the gap up-front and route around it.

    This dataclass is that contract. Every adapter exposes a
    :data:`PlatformAdapter.capabilities` instance; callers that depend on
    a capability check the flag *before* dispatching and either select a
    different adapter or degrade with a typed warning (see
    :func:`require_tool_scoping`) instead of silently granting full tools.

    Fields:
        supports_tool_scoping: ``True`` iff the adapter can *enforce* the
            ``AgentInvocation.allowed_tools`` allow-list at the CLI level
            (Claude Code's ``--allowed-tools``). ``False`` means the
            allow-list is advisory only — the adapter cannot prevent the
            underlying agent from using any tool (the Cursor CLI has no
            equivalent flag). Conservative default is ``False`` so a new
            adapter that forgets to declare the capability is treated as
            *unable* to scope rather than silently trusted.
    """

    supports_tool_scoping: bool = False


def require_tool_scoping(
    adapter: PlatformAdapter,
    *,
    role: str,
    allowed_tools: list[str] | None,
) -> bool:
    """Caller-side guard: is tool-scoping enforceable for this dispatch?

    Returns ``True`` when the caller's ``allowed_tools`` intent will be
    *enforced* by ``adapter`` — i.e. the caller can proceed knowing the
    constraint is real. Returns ``False`` when scoping was requested but
    the adapter cannot enforce it; in that case a single typed warning
    (``adapter.tool_scoping_unenforceable``) is emitted so the gap is
    observable, and the caller is expected to degrade (e.g. select a
    scoping-capable adapter, or accept the unscoped run knowingly) rather
    than *silently* granting full tools.

    ``allowed_tools is None`` means "no scoping requested" — there is
    nothing to enforce, so this returns ``True`` (vacuously satisfied)
    and emits no warning regardless of the adapter's capability. Only an
    explicit allow-list (including the empty ``[]`` "no tools" intent)
    triggers the capability check.
    """
    if allowed_tools is None:
        return True
    if adapter.capabilities.supports_tool_scoping:
        return True
    logger.warning(
        "adapter.tool_scoping_unenforceable",
        adapter=adapter.name,
        role=role,
        allowed_tools=allowed_tools,
        note=(
            "caller requested tool-scoping but this adapter cannot enforce "
            "an allow-list; degrade or select a scoping-capable adapter "
            "instead of silently granting full tools"
        ),
    )
    return False


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

    # WS3 (stabilization-v1): declared, machine-checkable capabilities.
    # Conservative default — a subclass that does not override this is
    # treated as UNABLE to enforce tool-scoping (and any other future
    # capability that defaults off). Concrete adapters override with their
    # real capability set (Claude Code → ``supports_tool_scoping=True``).
    capabilities: AdapterCapabilities = AdapterCapabilities()

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
