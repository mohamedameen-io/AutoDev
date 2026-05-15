"""Top-level orchestrator that wires state, adapters, agents together.

Responsibilities:
  - :meth:`plan` drives the plan-drafting FSM
    (:mod:`orchestrator.plan_phase`).
  - :meth:`execute` drives the per-task execute loop
    (:mod:`orchestrator.execute_phase`).
  - :meth:`resume` continues an in-progress execution from the ledger.
  - :meth:`status` produces a JSON-serializable snapshot for the CLI.

Note: the impl-tournament module exists and works via the dedicated CLI
surface, but is NOT yet integrated into :func:`execute_phase` itself.
Hooks left in the plan/execute modules with ``TODO(v0.26+)`` markers.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from adapters.base import PlatformAdapter
from adapters.types import AgentSpec
from config.schema import AutodevConfig
from guardrails import GuardrailEnforcer, LoopDetector
from autologging import attach_session_file_sink, get_logger
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
        # v0.25.2: open .autodev/sessions/<sid>/events.jsonl for the
        # lifetime of this Orchestrator so ``autodev logs`` can tail
        # structured events. Idempotent across re-entry on the same sid.
        attach_session_file_sink(self._session_id, self._cwd)
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
        # v0.17.0 S5: lazy-init slot for the project's tracked-files set.
        # Populated on first access via ``git ls-files``. ``None`` means
        # "not yet probed". Used by :func:`orchestrator.dag.find_file_overlaps`
        # and :func:`orchestrator.dag.validate_edit_scope` for glob
        # expansion of ``Task.files`` entries.
        self._tracked_files: set[str] | None = None
        # Phase 2 (anti-bloat): one-shot guard so :meth:`_seed_hive_packs` only
        # runs once per orchestrator instance even if it is called from
        # multiple entry points (plan / execute / resume).
        self._seed_packs_loaded: bool = False
        # v0.29.0 Bug 6: transient cache of the most recent adapter
        # result's ``subtype`` (and ``api_error_status``). Updated by
        # :func:`execute_phase.delegate` after every adapter call;
        # consumed by the ``GuardrailExceededError`` block sites in
        # :func:`execute_phase._execute_one` to classify the typed
        # ``Task.block_reason_class`` (auth/transport-class subtypes
        # → ``"infrastructure"``; everything else → ``"cap"``). NOT
        # persisted — a fresh orchestrator starts both at ``None``.
        self._last_adapter_subtype: str | None = None
        self._last_adapter_api_error_status: int | None = None

        # v0.30.0 Bug 5: cross-task infrastructure-failure circuit breaker.
        # Counts adapter failures with infra-class subtypes
        # (``auth_failed`` / ``rate_limited`` / ``server_error``) in a
        # rolling window; trips when the count crosses
        # ``cfg.circuit_breaker_threshold`` within
        # ``cfg.circuit_breaker_window_s`` seconds. The orchestrator's
        # :func:`execute_phase.delegate` site feeds every adapter result
        # in (success → ``reset()``; infra failure → ``record_failure``);
        # on a trip it raises :class:`InfrastructureCircuitOpenError`
        # which the v0.29.0 ``AuthenticationFailedError`` catch sites
        # treat identically (quarantine + paused-phase + non-zero exit).
        # Stored on the orchestrator (NOT lazy-init in execute()) so a
        # caller can swap it out for a fake in tests via attribute
        # assignment, mirroring how the rest of the orchestrator's
        # collaborator wiring works.
        from orchestrator.circuit_breaker import InfraFailureCircuitBreaker

        self._circuit_breaker = InfraFailureCircuitBreaker(
            threshold=cfg.circuit_breaker_threshold,
            window_s=cfg.circuit_breaker_window_s,
        )

        # v0.31.0 (Phase 3): per-(task_id, role) consecutive
        # ``error_max_turns`` tracker. The ``delegate()`` site reads
        # this BEFORE every dispatch to decide whether to escalate the
        # invocation's ``max_turns`` / ``timeout_s`` budget, and
        # updates it AFTER every dispatch with the adapter's
        # ``result.subtype``. Owned by the orchestrator (one tracker
        # per instance) so a fresh session resets the ladder by design.
        # See :mod:`orchestrator.budget_escalation`.
        from orchestrator.budget_escalation import BudgetEscalationTracker

        self._budget_escalation_tracker = BudgetEscalationTracker()

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
    def tracked_files(self) -> set[str]:
        """Return the cached set of repo-relative tracked files.

        v0.17.0 S5: lazy-populated via ``git ls-files`` on first access.
        Used by :func:`orchestrator.dag.find_file_overlaps` and
        :func:`orchestrator.dag.validate_edit_scope` to expand glob entries
        in ``Task.files`` against the project's actual file set.

        Empty set on first access if the repo has no tracked files (e.g.
        a fresh ``git init`` with nothing committed). The probe is
        idempotent: subsequent accesses return the same set object so
        callers can rely on identity-based caching.

        Refresh strategy: cached for the orchestrator's lifetime. New
        files added mid-session are NOT visible until the next session
        — by design, mirrors how :attr:`repo_capacity` snapshots the
        probe at orchestrator construction time.
        """
        if self._tracked_files is None:
            import subprocess

            try:
                proc = subprocess.run(
                    ["git", "ls-files"],
                    cwd=self._cwd,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                lines = proc.stdout.splitlines()
                self._tracked_files = {ln for ln in lines if ln}
            except (subprocess.CalledProcessError, FileNotFoundError):
                # Not a git repo, or git unavailable — empty set.
                self._tracked_files = set()
        return self._tracked_files

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

    async def _seed_hive_packs(self) -> None:
        """Phase 2 (anti-bloat): bootstrap hive-tier knowledge once per session.

        Called from each high-level entry point (``plan`` / ``execute`` /
        ``resume``) and short-circuits via :attr:`_seed_packs_loaded` so it
        runs at most once per orchestrator instance. The underlying
        :func:`state.seed_packs.seed_pack_if_missing` is itself idempotent
        across runs (marker file + Jaccard dedup), so the per-instance
        guard is purely a performance hint.

        Pack files live under ``<repo_root>/seeds/<name>.jsonl`` where
        ``repo_root`` is resolved relative to this module's location. If a
        configured pack file is missing the loader simply returns 0; we do
        not raise.
        """
        if self._seed_packs_loaded:
            return
        self._seed_packs_loaded = True
        kcfg = self._cfg.knowledge
        if not kcfg.seed_packs_enabled or not kcfg.seed_packs:
            return
        from state.seed_packs import seed_pack_if_missing

        # repo_root is the package install root (parent of ``src/``).
        # ``__file__`` is .../src/orchestrator/__init__.py -> parents[2] = repo root.
        repo_root = Path(__file__).resolve().parents[2]
        seeds_dir = repo_root / "seeds"
        marker_dir = self._cwd / ".autodev"
        for pack_name in kcfg.seed_packs:
            pack_path = seeds_dir / f"{pack_name}.jsonl"
            try:
                inserted = await seed_pack_if_missing(
                    self._knowledge,
                    pack_path,
                    pack_name,
                    marker_dir=marker_dir,
                )
                if inserted:
                    self._log.info(
                        "orchestrator.seed_pack.loaded",
                        pack=pack_name,
                        inserted=inserted,
                    )
            except Exception as exc:  # noqa: BLE001 - seeding is best-effort
                self._log.warning(
                    "orchestrator.seed_pack.failed",
                    pack=pack_name,
                    error=str(exc),
                )

    # --- High-level operations ---

    async def plan(self, intent: str) -> Plan:
        """Run the plan phase to completion. Returns the approved plan."""
        # Local import breaks a module cycle.
        from orchestrator.plan_phase import run_plan_phase

        # v0.13.0: trigger the lazy probe at the entry point so the
        # snapshot is available to downstream callers (delegate's
        # ``resolve_task_max_turns`` reads it).
        _ = self.repo_capacity
        await self._seed_hive_packs()

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
        await self._seed_hive_packs()

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

        v0.26.0: the inline-adapter suspend-state branch (read
        ``.autodev/inline-state.json``, validate the pending response
        file, clear the state) was removed alongside InlineAdapter.
        Every adapter is now subprocess; resume just picks up the
        ledger's first non-terminal task.

        v0.29.0 Bug 7: ``quarantined`` tasks are non-terminal so
        :func:`_find_in_progress_task` returns them — the existing
        retry-in-progress branch walks them back through ``in_progress``
        and re-dispatches. Additionally, when no quarantined tasks
        remain in a phase that was previously parked at
        ``review_status="paused"`` (because an earlier auth_failed halt
        forced the phase aggregator to bail), we clear the paused state
        and re-enter the execute loop so ``_maybe_run_phase_review``
        re-fires the phase-review tournament fresh.
        """
        from orchestrator.execute_phase import run_execute_phase

        # v0.13.0: probe lazily on resume entry (mirrors plan/execute).
        _ = self.repo_capacity
        await self._seed_hive_packs()

        plan = await self._plan_manager.load()
        if plan is None:
            self._log.warning("orchestrator.resume.no_plan")
            return []

        # v0.29.0 Bug 7: clear ``review_status="paused"`` for any phase
        # whose only blocking signal was quarantined work that has now
        # been resolved (or that we are about to re-dispatch). Done
        # BEFORE the in-flight scan so the post-dispatch
        # ``_maybe_run_phase_review`` poll sees a clean slate and
        # re-fires the tournament fresh. ``update_phase_meta`` treats
        # ``None`` as "leave unchanged"; we emit the clear-op directly
        # (mirrors the pattern used by ``PlanManager.requeue_tasks``).
        paused_phase_ids: list[str] = [
            p.id for p in plan.phases if p.review_status == "paused"
        ]
        if paused_phase_ids:
            from state.lockfile import plan_lock as _plan_lock
            from state.ledger import append_entry as _append_entry

            for phase_id in paused_phase_ids:
                self._log.info(
                    "orchestrator.resume.clear_paused_phase",
                    phase_id=phase_id,
                )
                async with _plan_lock(self._plan_manager.cwd, timeout_s=30.0):
                    await _append_entry(
                        self._plan_manager.cwd,
                        op="update_phase_meta",
                        payload={
                            "phase_id": phase_id,
                            "review_status": None,
                        },
                        session_id=self._session_id,
                    )
            # Re-load so the post-clear plan is what we route on.
            plan = await self._plan_manager.load()
            if plan is None:
                self._log.warning("orchestrator.resume.no_plan_post_clear")
                return []

        in_progress = _find_in_progress_task(plan)
        if in_progress is not None:
            # Re-seed status so the execute loop will pick it up as pending.
            # We intentionally leave its retry_count alone.
            self._log.info(
                "orchestrator.resume.retry_in_progress",
                task_id=in_progress.id,
                prior_status=in_progress.status,
            )
            # Mark it in_progress -> in_progress is a legal self-loop.
            # But to trigger the pending-scan, briefly park it at pending.
            # v0.29.0 Bug 7: ``quarantined`` -> ``in_progress`` is the
            # documented resume edge (see :data:`TASK_TRANSITIONS`).
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
                # v0.29.0 Bug 7: ``quarantined`` is non-terminal — a task
                # halted by an infrastructure failure (auth_failed etc.)
                # is eligible to resume once the operator clears the
                # underlying issue. Including it here means
                # ``Orchestrator.resume()`` picks the task up
                # automatically without operator intervention.
                "quarantined",
            ):
                return task
    return None


__all__ = ["Orchestrator"]
