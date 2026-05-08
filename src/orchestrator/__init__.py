"""Top-level orchestrator that wires state, adapters, agents together.

Phase 4 responsibilities:
  - :meth:`plan` drives the plan-drafting FSM
    (:mod:`orchestrator.plan_phase`).
  - :meth:`execute` drives the per-task execute loop
    (:mod:`orchestrator.execute_phase`).
  - :meth:`resume` continues an in-progress execution from the ledger.
  - :meth:`status` produces a JSON-serializable snapshot for the CLI.

Tournaments are NOT integrated in Phase 4. Hooks in the plan/execute
modules leave ``TODO(phase-6)`` / ``TODO(phase-7)`` markers.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from adapters.base import PlatformAdapter
from adapters.types import AgentSpec
from config.schema import AutodevConfig
from guardrails import GuardrailEnforcer, LoopDetector
from autologging import get_logger
from orchestrator.prm import TrajectoryStore
from plugins.registry import PluginRegistry
from runtime.repo_probe import RepoCapacity, probe_repo
from state.knowledge import KnowledgeStore
from state.plan_manager import PlanManager
from state.schemas import Plan, Task


logger = get_logger(__name__)


class Orchestrator:
    """Glue between config, state, adapter, and agent registry."""

    def __init__(
        self,
        cwd: Path,
        cfg: AutodevConfig,
        adapter: PlatformAdapter,
        registry: dict[str, AgentSpec],
        session_id: str | None = None,
        *,
        disable_impl_tournament: bool = False,
        lock_timeout_s: float = 30.0,
        plugin_registry: PluginRegistry | None = None,
    ) -> None:
        self._cwd = Path(cwd)
        self._cfg = cfg
        self._adapter = adapter
        self._registry = registry
        self._session_id = session_id or f"sess-{uuid.uuid4().hex[:12]}"
        self._disable_impl_tournament = disable_impl_tournament
        self._plan_manager = PlanManager(
            self._cwd, self._session_id, lock_timeout_s=lock_timeout_s
        )
        self._knowledge = KnowledgeStore(self._cwd, cfg=cfg)
        self._log = get_logger(component="orchestrator", session_id=self._session_id)
        self.guardrails = GuardrailEnforcer(cfg.guardrails)
        self.loop_detector = LoopDetector()
        self.plugin_registry: PluginRegistry | None = plugin_registry

        # v0.13.0: lazy-init slot for the repo-size snapshot. The probe is
        # cheap (~10ms typical, up to ~1s on Unity-class repos) so we run it
        # on the first plan()/execute() entry and cache the result for the
        # session. ``None`` means "not yet probed"; populated once any of
        # the high-level operations is invoked.
        self._repo_capacity: RepoCapacity | None = None
        # v0.15.0: PRM trajectory store. Records every delegate dispatch
        # for pattern detection. In-memory only (mirrors the rest of
        # v0.15.0's ladder design).
        self._trajectory_store = TrajectoryStore()

        # Wire AgentExtensionPlugins: merge their specs into the agent registry.
        if plugin_registry is not None:
            for plugin in plugin_registry.agents.values():
                try:
                    spec = plugin.get_spec()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "orchestrator.plugin_agent_spec_error",
                        plugin=plugin.name,
                        error=str(exc),
                    )
                    continue
                if spec is None:
                    continue
                # ``spec`` is duck-typed as AgentSpec-compatible.
                # If it's already an AgentSpec, use it directly; otherwise
                # try to treat it as a dict or object with the needed attrs.
                if isinstance(spec, AgentSpec):
                    self._registry[spec.name] = spec
                elif isinstance(spec, dict):
                    try:
                        agent_spec = AgentSpec(**spec)
                        self._registry[agent_spec.name] = agent_spec
                    except Exception as exc2:  # noqa: BLE001
                        logger.warning(
                            "orchestrator.plugin_agent_spec_invalid",
                            plugin=plugin.name,
                            error=str(exc2),
                        )
                else:
                    # Duck-typed object: try to build AgentSpec from its attributes.
                    try:
                        agent_spec = AgentSpec(
                            name=spec.name,
                            description=getattr(spec, "description", ""),
                            prompt=getattr(spec, "prompt", ""),
                            tools=list(getattr(spec, "tools", [])),
                            model=getattr(spec, "model", None),
                            max_turns=getattr(spec, "max_turns", None),
                        )
                        self._registry[agent_spec.name] = agent_spec
                    except Exception as exc3:  # noqa: BLE001
                        logger.warning(
                            "orchestrator.plugin_agent_spec_invalid",
                            plugin=plugin.name,
                            error=str(exc3),
                        )

    # --- Accessors (kept public for plan_phase/execute_phase modules) ---

    @property
    def cwd(self) -> Path:
        return self._cwd

    @property
    def cfg(self) -> AutodevConfig:
        return self._cfg

    @property
    def adapter(self) -> PlatformAdapter:
        return self._adapter

    @property
    def registry(self) -> dict[str, AgentSpec]:
        return self._registry

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def plan_manager(self) -> PlanManager:
        return self._plan_manager

    @property
    def knowledge(self) -> KnowledgeStore:
        return self._knowledge

    @property
    def trajectory_store(self) -> TrajectoryStore:
        """v0.15.0 PRM trajectory store (in-memory, per-orchestrator)."""
        return self._trajectory_store

    @property
    def disable_impl_tournament(self) -> bool:
        return self._disable_impl_tournament

    @property
    def repo_capacity(self) -> RepoCapacity:
        """Return the cached repo-size snapshot, probing lazily on first access.

        v0.13.0: orchestrator entry points (``plan``/``execute``/``resume``)
        and the ``delegate`` site read this property. The probe runs at
        most once per Orchestrator instance; subsequent reads return the
        cached :class:`RepoCapacity`.
        """
        if self._repo_capacity is None:
            # Resolve the probe through the orchestrator package's import
            # surface so tests can monkeypatch ``orchestrator.probe_repo``
            # to substitute a fake without having to also patch the
            # underlying ``runtime.repo_probe`` module.
            import orchestrator as _orch_mod

            probe = getattr(_orch_mod, "probe_repo", probe_repo)
            self._repo_capacity = probe(self._cwd)
        return self._repo_capacity

    # --- High-level operations ---

    async def plan(self, intent: str) -> Plan:
        """Run the plan phase to completion. Returns the approved plan."""
        # Local import breaks a module cycle.
        from orchestrator.plan_phase import run_plan_phase

        # v0.13.0: trigger the lazy probe at the entry point so the
        # snapshot is available to downstream callers (delegate's
        # ``resolve_task_max_turns`` reads it).
        _ = self.repo_capacity

        self._log.info("orchestrator.plan.start", intent_bytes=len(intent))
        plan = await run_plan_phase(self, intent)
        self._log.info(
            "orchestrator.plan.done",
            plan_id=plan.plan_id,
            phases=len(plan.phases),
        )
        return plan

    async def execute(self, task_id: str | None = None) -> list[Task]:
        """Run execute-phase loop. Returns the list of tasks processed."""
        from orchestrator.execute_phase import run_execute_phase

        # v0.13.0: probe lazily on first entry; downstream delegate site
        # reads ``self._repo_capacity`` to resolve per-task max_turns.
        _ = self.repo_capacity

        self._log.info("orchestrator.execute.start", task_id=task_id or "<all-pending>")
        tasks = await run_execute_phase(self, task_id)
        self._log.info(
            "orchestrator.execute.done",
            processed=len(tasks),
            complete=sum(1 for t in tasks if t.status == "complete"),
            blocked=sum(1 for t in tasks if t.status == "blocked"),
        )
        return tasks

    async def resume(self) -> list[Task]:
        """Re-enter the execute loop from wherever the ledger left off.

        Finds the first non-terminal task (any status other than
        ``complete``/``skipped``/``blocked``) and drives the execute loop
        from there. For Phase 4 that is effectively the same as
        :meth:`execute` with ``task_id=None`` because the loop itself picks
        up the first pending task.

        For the inline adapter, validates that the pending response file
        exists before continuing and clears the suspend state file.
        """
        from adapters.inline import InlineAdapter
        from errors import AutodevError as _AutodevError
        from orchestrator.execute_phase import run_execute_phase
        from orchestrator.inline_state import (
            clear_suspend_state,
            load_suspend_state,
        )

        # v0.13.0: probe lazily on resume entry (mirrors plan/execute).
        _ = self.repo_capacity

        if isinstance(self._adapter, InlineAdapter):
            state = load_suspend_state(self._cwd)
            if state is not None:
                if not self._adapter.has_pending_response(
                    state.pending_task_id, state.pending_role
                ):
                    raise _AutodevError(
                        f"Response file not yet written for "
                        f"{state.pending_task_id}/{state.pending_role}. "
                        f"Agent must complete the delegation first. "
                        f"Check: .autodev/delegations/"
                        f"{state.pending_task_id}-{state.pending_role}.md"
                    )
                clear_suspend_state(self._cwd)
                # Continue with normal resume — the execute loop will pick up
                # from the ledger checkpoint and the delegate() inline-resume
                # path will collect the response file.

        plan = await self._plan_manager.load()
        if plan is None:
            self._log.warning("orchestrator.resume.no_plan")
            return []

        in_progress = _find_in_progress_task(plan)
        if in_progress is not None:
            # Re-seed status so the execute loop will pick it up as pending.
            # We intentionally leave its retry_count alone.
            self._log.info(
                "orchestrator.resume.retry_in_progress",
                task_id=in_progress.id,
            )
            # Mark it in_progress -> in_progress is a legal self-loop.
            # But to trigger the pending-scan, briefly park it at pending.
            await self._plan_manager.update_task_status(in_progress.id, "in_progress")
            # Drive the execute loop for this specific task so it is picked
            # up regardless of next_pending_task() filtering.
            return await run_execute_phase(self, task_id=in_progress.id)

        return await run_execute_phase(self, task_id=None)

    async def status(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot for the CLI."""
        plan = await self._plan_manager.load()
        if plan is None:
            return {
                "plan": None,
                "session_id": self._session_id,
                "tasks": [],
            }
        tasks: list[dict[str, Any]] = []
        for phase in plan.phases:
            for task in phase.tasks:
                tasks.append(
                    {
                        "id": task.id,
                        "phase_id": task.phase_id,
                        "title": task.title,
                        "status": task.status,
                        "retry_count": task.retry_count,
                        "escalated": task.escalated,
                        "evidence_bundle": task.evidence_bundle,
                    }
                )
        return {
            "plan": {
                "plan_id": plan.plan_id,
                "spec_hash": plan.spec_hash,
                "phases": len(plan.phases),
                "title": plan.metadata.get("title", ""),
                "created_at": plan.created_at,
                "updated_at": plan.updated_at,
            },
            "session_id": self._session_id,
            "tasks": tasks,
            "totals": {
                "pending": sum(1 for t in tasks if t["status"] == "pending"),
                "in_progress": sum(1 for t in tasks if t["status"] == "in_progress"),
                "complete": sum(1 for t in tasks if t["status"] == "complete"),
                "blocked": sum(1 for t in tasks if t["status"] == "blocked"),
                "total": len(tasks),
            },
        }


def _find_in_progress_task(plan: Plan) -> Task | None:
    for phase in plan.phases:
        for task in phase.tasks:
            if task.status in (
                "in_progress",
                "coded",
                "auto_gated",
                "reviewed",
                "tested",
                "tournamented",
            ):
                return task
    return None


__all__ = ["Orchestrator"]
