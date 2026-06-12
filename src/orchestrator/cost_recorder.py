"""Phase 0 (cost/time telemetry): single-chokepoint per-invocation cost capture.

The orchestrator drives agent invocations through several surfaces:

  * the main delegate path (:func:`orchestrator.plan_phase.delegate` /
    :func:`orchestrator.execute_phase.delegate`), which already routes its
    :class:`~adapters.types.AgentResult` through
    :meth:`guardrails.enforcer.GuardrailEnforcer.post_invocation` (and so
    accumulates ``plan_cost_usd``);
  * every tournament surface — judges via
    :class:`tournament.llm.AdapterLLMClient` and the impl-tournament
    developers / test_engineers via
    :class:`orchestrator.impl_tournament_runner._CoderRunner` — which call
    ``adapter.execute`` directly and **bypass** the enforcer entirely.

Tournament invocations dominate spend (a single impl tournament fans out
N developer variants × M judges over multiple rounds), so the in-memory
``plan_cost_usd`` total under-counts the real run cost. The one place
EVERY invocation passes through is the orchestrator's single shared
adapter instance (``orch.adapter``): the main path uses it, and every
``AdapterLLMClient`` / ``_CoderRunner`` is constructed with it.

:class:`CostRecordingAdapter` is a thin, transparent decorator placed
around that shared adapter in :meth:`Orchestrator.__init__`. It delegates
``execute`` to the wrapped adapter and, after each call, appends one
audit-only ``invocation_cost`` ledger op against the **main-repo** cwd /
session (NOT ``inv.cwd``, which is a throw-away worktree for tournament
variants). Summing those ops over a run window yields the authoritative
total cost — tournaments included.

Everything else (``name``, ``last_pid``, ``init_workspace``,
``healthcheck``, ``parallel``, …) is forwarded verbatim via ``__getattr__``
so the wrapper is behaviourally indistinguishable from the real adapter
for every existing call site.

Best-effort by construction: a ledger-append failure here is swallowed and
NEVER masks the adapter result the caller is waiting on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from adapters.base import PlatformAdapter
from adapters.types import AgentInvocation, AgentResult
from autologging import get_logger

if TYPE_CHECKING:
    from state.plan_manager import PlanManager


log = get_logger(__name__)


class CostRecordingAdapter(PlatformAdapter):
    """Transparent cost-capturing decorator around a :class:`PlatformAdapter`.

    Args:
        inner: the real adapter every invocation is delegated to.
        plan_manager: the orchestrator's plan-manager — used to append the
            ``invocation_cost`` ledger op against the main repo / session.
    """

    def __init__(self, inner: PlatformAdapter, plan_manager: "PlanManager") -> None:
        self._inner = inner
        self._plan_manager = plan_manager
        # Mirror the wrapped adapter's name so telemetry / fitness logging
        # that reads ``adapter.name`` keeps reporting the real backend.
        self.name = getattr(inner, "name", "abstract")

    async def init_workspace(self, cwd: Any, agents: list[Any]) -> None:
        await self._inner.init_workspace(cwd, agents)

    async def execute(self, inv: AgentInvocation) -> AgentResult:
        """Delegate to the inner adapter, then emit an ``invocation_cost`` op.

        The ledger append is best-effort: any failure is logged and
        swallowed so telemetry can never break an invocation.
        """
        result = await self._inner.execute(inv)
        await self._record_cost(inv, result)
        return result

    async def healthcheck(self) -> tuple[bool, str]:
        return await self._inner.healthcheck()

    async def _record_cost(self, inv: AgentInvocation, result: AgentResult) -> None:
        try:
            await self._plan_manager.ledger_append(
                op="invocation_cost",
                payload={
                    "role": getattr(inv, "role", None),
                    "task_id": getattr(inv, "task_id", None),
                    "cost_usd": float(getattr(result, "cost_usd", 0.0) or 0.0),
                    "duration_s": float(getattr(result, "duration_s", 0.0) or 0.0),
                },
            )
        except Exception as exc:  # noqa: BLE001 — telemetry must never fail a run
            log.warning("cost_recorder.ledger_append_failed", err=str(exc))

    def __getattr__(self, name: str) -> Any:
        """Forward any unknown attribute access to the wrapped adapter.

        Covers adapter-specific surface the wrapper does not override —
        e.g. ``last_pid`` (read by :class:`tournament.llm.AdapterLLMClient`)
        and any future adapter attribute. ``__getattr__`` only fires for
        names not found on the instance / class, so the overridden methods
        above and ``name`` are never shadowed.
        """
        # ``_inner`` is set in ``__init__``; guard against the pre-init
        # window (e.g. unpickling) to avoid infinite recursion.
        if name == "_inner":
            raise AttributeError(name)
        return getattr(self._inner, name)


__all__ = ["CostRecordingAdapter"]
