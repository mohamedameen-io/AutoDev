"""WS5 — ``ask_human`` dead-end → explicit best-effort-commit / fail path.

Root cause (observed across the whole slice): every deterministic recovery
ladder terminates at ``ask_human`` (``orchestrator.blocker_resolver``), but
``_apply_resolution`` explicitly DECLINES it (``return None`` for
``ask_human``/``fall_through``/``web_search``/``reroute``). There is no pause,
no timeout, no surfacing mechanism an unattended run would ever see — the run
silently blocks. The resolver's own prompt falsely claimed the orchestrator
"routes ``ask_human`` to the same human-decision channel."

Fix: a new opt-in config ``ResolverConfig.on_ask_human`` with three modes:

* ``"block"`` (DEFAULT — a pure no-op vs. today, the regression pin): the
  ladder's ``ask_human`` still falls through to the caller's legacy block.
* ``"best_effort_commit"``: when the ladder would resolve to ``ask_human``,
  attempt to apply whatever diff currently exists in the task's worktree; if
  it is non-empty AND it applies, mark the task ``complete`` via the shared
  FSM-walk helper, stamped with unambiguous ``needs_human_review`` metadata and
  a distinctly-named ``best_effort_committed_on_ask_human`` ledger op so a
  benchmark scorer can treat it as its OWN terminal category (not "solved").
  Nothing to commit (empty diff / no worktree) OR an apply that fails →
  unchanged (falls through to the legacy block; the apply succeeding or failing
  IS the safety check — the apply is NEVER forced).
* ``"fail"``: raise :class:`errors.AskHumanDeadEndError` loudly at the point the
  ladder resolves to ``ask_human`` — a benchmark harness that would rather see a
  hard failure than silently block.

Test plan (from the roadmap):
  * reproduce the real ladder trace terminating at ``ask_human``;
  * assert default (``"block"``) behaviour is unchanged (regression pin);
  * assert ``"best_effort_commit"`` produces the new terminal state ONLY when a
    non-empty diff exists and applies;
  * config back-compat for the new field's default on legacy configs.
"""

from __future__ import annotations

import datetime as _dt
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agents import build_registry
from config.defaults import default_config
from config.schema import ResolverConfig
from errors import AskHumanDeadEndError
from orchestrator import Orchestrator
from orchestrator import blocker_resolver as br
from orchestrator import execute_phase as ep
from orchestrator import failure_classes as fc
from state import ledger as ledger_mod
from state.plan_manager import PlanManager
from state.schemas import (
    AcceptanceCriterion,
    BlockerContext,
    Phase,
    Plan,
    ResolutionAction,
    Task,
)

from stub_adapter import StubAdapter, fail, ok

pytestmark = pytest.mark.resolver_enabled


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan() -> Plan:
    task = Task(
        id="1.1",
        phase_id="1",
        title="Implement widget",
        description="Do the work.",
        files=["widget.py"],
        acceptance=[AcceptanceCriterion(id="ac-1", description="works")],
    )
    return Plan(
        plan_id="p-ws5",
        spec_hash="deadbeef",
        phases=[Phase(id="1", title="Build", tasks=[task])],
        created_at=_iso(),
        updated_at=_iso(),
    )


class _FakeWorktreeMgr:
    """Minimal worktree manager double for the best-effort-commit apply head.

    ``get_diff_vs_base`` returns a configurable diff (or raises), and
    ``apply_patch_to_main`` succeeds or raises. Records what happened so tests
    can assert the apply was (or was not) attempted.
    """

    def __init__(
        self,
        *,
        diff: str = "",
        apply_ok: bool = True,
        diff_raises: bool = False,
    ) -> None:
        self._diff = diff
        self._apply_ok = apply_ok
        self._diff_raises = diff_raises
        self.diff_calls = 0
        self.apply_calls = 0

    async def get_diff_vs_base(self, worktree: Any, base_ref: str = "HEAD") -> str:
        self.diff_calls += 1
        if self._diff_raises:
            from orchestrator.worktree import WorktreeError

            raise WorktreeError("diff-check failed")
        return self._diff

    async def apply_patch_to_main(
        self,
        worktree: Any,
        base_ref: str = "HEAD",
        three_way: bool = False,
        commit_message: str | None = None,
        **_: Any,
    ) -> None:
        self.apply_calls += 1
        if not self._apply_ok:
            from orchestrator.worktree import WorktreeError

            raise WorktreeError("apply conflict")


async def _mk_orch(tmp_path: Path, *, on_ask_human: str = "block") -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    cfg.tournaments.review_tournament_enabled = False
    cfg.qa_retry_min_interval_s = 0.0
    cfg.resolver.on_ask_human = on_ask_human  # type: ignore[assignment]
    registry = build_registry(cfg)
    orch = Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=StubAdapter({}),
        registry=registry,
        session_id="ws5-sess",
    )
    await orch.plan_manager.init_plan(_mk_plan())
    await orch.plan_manager.update_task_status("1.1", "in_progress")
    return orch


async def _task(orch: Orchestrator, task_id: str = "1.1") -> Task:
    plan = await orch.plan_manager.load()
    assert plan is not None
    for phase in plan.phases:
        for t in phase.tasks:
            if t.id == task_id:
                return t
    raise AssertionError(f"task {task_id} not found")


def _ask_human_action() -> ResolutionAction:
    return ResolutionAction(
        action="ask_human",
        params={"question": "Which API version is authoritative?"},
        rationale="ladder exhausted; a human decision is genuinely required",
    )


def _ctx() -> BlockerContext:
    return BlockerContext(
        failure_class=fc.WORKER_EXCEPTION,
        task_id="1.1",
        phase_id="1",
        failing_role="developer",
    )


def _ops(cwd: Path) -> list[str]:
    return [e.op for e in ledger_mod.read_entries(cwd)]


# ===========================================================================
# 1. Config back-compat
# ===========================================================================


def test_default_on_ask_human_is_block() -> None:
    """The new field defaults to ``"block"`` — preserves today's behaviour."""
    cfg = default_config()
    assert cfg.resolver.on_ask_human == "block"


def test_legacy_config_dict_without_on_ask_human_validates_to_block() -> None:
    """A legacy resolver config (no ``on_ask_human`` key) still validates under
    ``extra="forbid"`` and defaults to ``"block"`` (back-compat)."""
    legacy = {
        "enabled": True,
        "max_cycles_per_blocker": 3,
        "max_corrective_cycles_per_phase": 3,
        "fast_path_only_on_known": True,
        "model": None,
    }
    cfg = ResolverConfig.model_validate(legacy)
    assert cfg.on_ask_human == "block"


def test_on_ask_human_rejects_unknown_mode() -> None:
    """The Literal is enforced — a typo is a validation error, not silent."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ResolverConfig.model_validate(
            {"enabled": True, "on_ask_human": "commit_everything"}
        )


# ===========================================================================
# 2. Shared FSM-walk helper: _walk_task_to_complete
# ===========================================================================


@pytest.mark.asyncio
async def test_walk_to_complete_from_in_progress(tmp_path: Path) -> None:
    """The shared helper walks in_progress → complete, stamps completion meta
    onto the task, and emits the named ledger op."""
    orch = await _mk_orch(tmp_path)
    task = await _task(orch)
    assert task.status == "in_progress"

    completed = await ep._walk_task_to_complete(
        orch,
        task,
        ledger_op="best_effort_committed_on_ask_human",
        ledger_payload={"needs_human_review": True},
        complete_meta={"needs_human_review": True, "completion_reason": "ws5"},
        log_event="execute_phase.test_walk",
    )

    assert completed.status == "complete"
    assert completed.metadata.get("needs_human_review") is True
    assert completed.metadata.get("completion_reason") == "ws5"
    assert "best_effort_committed_on_ask_human" in _ops(tmp_path)


@pytest.mark.asyncio
async def test_walk_to_complete_from_mid_pipeline(tmp_path: Path) -> None:
    """The forward-walk is robust to a non-``in_progress`` start state (e.g.
    ``reviewed``): it only applies forward edges, never an illegal back-edge."""
    orch = await _mk_orch(tmp_path)
    # Drive the task forward to ``reviewed`` via legal edges.
    for st in ("coded", "auto_gated", "reviewed"):
        await orch.plan_manager.update_task_status("1.1", st)
    task = await _task(orch)
    assert task.status == "reviewed"

    completed = await ep._walk_task_to_complete(
        orch,
        task,
        ledger_op="best_effort_committed_on_ask_human",
        ledger_payload={},
        complete_meta={"needs_human_review": True},
        log_event="execute_phase.test_walk",
    )
    assert completed.status == "complete"


# ===========================================================================
# 3. _apply_resolution ask_human handling (the three modes)
# ===========================================================================


@pytest.mark.asyncio
async def test_ask_human_block_mode_returns_none(tmp_path: Path) -> None:
    """REGRESSION PIN: default ``"block"`` mode declines ask_human exactly as
    today — returns None (caller does its legacy block), never completes."""
    orch = await _mk_orch(tmp_path, on_ask_human="block")
    task = await _task(orch)
    fake = _FakeWorktreeMgr(diff="diff --git a/x b/x\n+work\n", apply_ok=True)

    result = await ep._apply_resolution(
        orch,
        task,
        _ctx(),
        _ask_human_action(),
        worktree=tmp_path,
        worktree_mgr=fake,
    )

    assert result is None
    # Never touched the worktree — block mode is inert.
    assert fake.diff_calls == 0
    assert fake.apply_calls == 0
    assert (await _task(orch)).status == "in_progress"


@pytest.mark.asyncio
async def test_ask_human_fail_mode_raises(tmp_path: Path) -> None:
    """``"fail"`` mode raises AskHumanDeadEndError loudly (no worktree needed)."""
    orch = await _mk_orch(tmp_path, on_ask_human="fail")
    task = await _task(orch)

    with pytest.raises(AskHumanDeadEndError):
        await ep._apply_resolution(
            orch, task, _ctx(), _ask_human_action(), worktree=None, worktree_mgr=None
        )


@pytest.mark.asyncio
async def test_best_effort_commit_nonempty_diff_completes(tmp_path: Path) -> None:
    """``"best_effort_commit"`` with a non-empty diff that applies → the task is
    completed with distinguishing ``needs_human_review`` metadata AND a
    distinctly-named ledger op (the scorer-visible new terminal category)."""
    orch = await _mk_orch(tmp_path, on_ask_human="best_effort_commit")
    task = await _task(orch)
    fake = _FakeWorktreeMgr(diff="diff --git a/widget.py b/widget.py\n+x\n")

    result = await ep._apply_resolution(
        orch, task, _ctx(), _ask_human_action(), worktree=tmp_path, worktree_mgr=fake
    )

    assert result is not None
    assert result.status == "complete"
    assert result.metadata.get("needs_human_review") is True
    assert fake.apply_calls == 1  # the diff was actually applied to main
    ops = _ops(tmp_path)
    assert "best_effort_committed_on_ask_human" in ops


@pytest.mark.asyncio
async def test_best_effort_commit_empty_diff_falls_through(tmp_path: Path) -> None:
    """Nothing to commit (empty worktree diff) → fall through to legacy block
    (return None); no completion, no apply."""
    orch = await _mk_orch(tmp_path, on_ask_human="best_effort_commit")
    task = await _task(orch)
    fake = _FakeWorktreeMgr(diff="   \n")  # whitespace-only == empty

    result = await ep._apply_resolution(
        orch, task, _ctx(), _ask_human_action(), worktree=tmp_path, worktree_mgr=fake
    )

    assert result is None
    assert fake.apply_calls == 0
    assert (await _task(orch)).status == "in_progress"
    assert "best_effort_committed_on_ask_human" not in _ops(tmp_path)


@pytest.mark.asyncio
async def test_best_effort_commit_apply_failure_falls_through(tmp_path: Path) -> None:
    """The apply succeeding or failing IS the safety check — never force. A
    non-empty diff that will NOT apply → fall through (return None), unchanged."""
    orch = await _mk_orch(tmp_path, on_ask_human="best_effort_commit")
    task = await _task(orch)
    fake = _FakeWorktreeMgr(
        diff="diff --git a/widget.py b/widget.py\n+x\n", apply_ok=False
    )

    result = await ep._apply_resolution(
        orch, task, _ctx(), _ask_human_action(), worktree=tmp_path, worktree_mgr=fake
    )

    assert result is None
    assert fake.apply_calls == 1  # attempted once, not forced/retried
    assert (await _task(orch)).status == "in_progress"
    assert "best_effort_committed_on_ask_human" not in _ops(tmp_path)


@pytest.mark.asyncio
async def test_best_effort_commit_no_worktree_falls_through(tmp_path: Path) -> None:
    """With no worktree available, best_effort_commit degrades to ``block``
    (return None) — the feature can only fire where a worktree exists."""
    orch = await _mk_orch(tmp_path, on_ask_human="best_effort_commit")
    task = await _task(orch)

    result = await ep._apply_resolution(
        orch, task, _ctx(), _ask_human_action(), worktree=None, worktree_mgr=None
    )
    assert result is None
    assert (await _task(orch)).status == "in_progress"


@pytest.mark.asyncio
async def test_best_effort_commit_diff_check_failure_falls_through(
    tmp_path: Path,
) -> None:
    """A worktree whose diff cannot even be READ → best-effort fall-through
    (never a hard block from this path)."""
    orch = await _mk_orch(tmp_path, on_ask_human="best_effort_commit")
    task = await _task(orch)
    fake = _FakeWorktreeMgr(diff_raises=True)

    result = await ep._apply_resolution(
        orch, task, _ctx(), _ask_human_action(), worktree=tmp_path, worktree_mgr=fake
    )
    assert result is None
    assert fake.apply_calls == 0


# ===========================================================================
# 4. Real ladder-trace root-cause pin
# ===========================================================================


def test_deterministic_ladder_terminates_at_ask_human() -> None:
    """Root-cause pin: every deterministic ladder's terminal rung is
    ``ask_human``. Reproduce the trace shape — a worker_exception whose
    retry_with_changes rung is already tried resolves to ``ask_human``."""
    ctx = BlockerContext(
        failure_class=fc.WORKER_EXCEPTION,
        task_id="1.1",
        recovery_already_tried=["retry_with_changes"],
    )
    action = br.deterministic_action(ctx)
    assert action is not None
    assert action.action == "ask_human"


# ===========================================================================
# 5. Integration seam: _maybe_resolve_blocker forced to ask_human
# ===========================================================================


async def _seed_budget_exhausted(orch: Orchestrator, blocker_key: str) -> None:
    """Pre-seed the ledger so ``count_prior_cycles`` >= max → resolve_blocker
    returns the budget-exhausted ``ask_human``."""
    for _ in range(int(orch.cfg.resolver.max_cycles_per_blocker)):
        await orch.plan_manager.ledger_append(
            op="resolution_chosen",
            payload={"blocker_key": blocker_key, "action": "retry_with_changes"},
        )


@pytest.mark.asyncio
async def test_maybe_resolve_blocker_best_effort_end_to_end(tmp_path: Path) -> None:
    """Drive the resolver chokepoint to ``ask_human`` (budget exhausted) with a
    non-empty worktree diff and ``best_effort_commit`` — it completes the task,
    stamps needs_human_review, and records the distinct ledger op + a
    ``recovered`` resolution_outcome."""
    orch = await _mk_orch(tmp_path, on_ask_human="best_effort_commit")
    await _seed_budget_exhausted(orch, "1.1:worker_exception")
    task = await _task(orch)
    fake = _FakeWorktreeMgr(diff="diff --git a/widget.py b/widget.py\n+x\n")

    recovered = await ep._maybe_resolve_blocker(
        orch,
        task,
        failure_class=fc.WORKER_EXCEPTION,
        raw_error="boom",
        worktree=tmp_path,
        worktree_mgr=fake,
    )
    assert recovered is not None
    assert recovered.status == "complete"
    assert recovered.metadata.get("needs_human_review") is True
    ops = _ops(tmp_path)
    assert "best_effort_committed_on_ask_human" in ops


@pytest.mark.asyncio
async def test_maybe_resolve_blocker_block_mode_end_to_end(tmp_path: Path) -> None:
    """REGRESSION PIN (seam level): the same forced-ask_human path in default
    ``block`` mode returns None (legacy block) — no completion, no besteffort op."""
    orch = await _mk_orch(tmp_path, on_ask_human="block")
    await _seed_budget_exhausted(orch, "1.1:worker_exception")
    task = await _task(orch)
    fake = _FakeWorktreeMgr(diff="diff --git a/widget.py b/widget.py\n+x\n")

    recovered = await ep._maybe_resolve_blocker(
        orch,
        task,
        failure_class=fc.WORKER_EXCEPTION,
        raw_error="boom",
        worktree=tmp_path,
        worktree_mgr=fake,
    )
    assert recovered is None
    assert fake.apply_calls == 0
    assert "best_effort_committed_on_ask_human" not in _ops(tmp_path)


@pytest.mark.asyncio
async def test_maybe_resolve_blocker_fail_mode_raises(tmp_path: Path) -> None:
    """``fail`` mode raises out of the chokepoint (not swallowed by the
    ``_apply_resolution`` guard)."""
    orch = await _mk_orch(tmp_path, on_ask_human="fail")
    await _seed_budget_exhausted(orch, "1.1:worker_exception")
    task = await _task(orch)

    with pytest.raises(AskHumanDeadEndError):
        await ep._maybe_resolve_blocker(
            orch,
            task,
            failure_class=fc.WORKER_EXCEPTION,
            raw_error="boom",
        )


# ===========================================================================
# 6. Real-loop regression + fail propagation (drives run_execute_phase)
# ===========================================================================


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True)
    (repo / "widget.py").write_text("def f():\n    return 1\n")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(repo), check=True)


def _real_cfg(on_ask_human: str) -> Any:
    cfg = default_config()
    cfg.tournaments.phase_review.enabled = False
    cfg.tournaments.impl.enabled = False
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.auto_disable_for_models = []
    cfg.qa_retry_min_interval_s = 0.0
    cfg.qa_retry_limit = 1
    cfg.resolver.on_ask_human = on_ask_human  # type: ignore[assignment]
    return cfg


async def _make_real_orch(repo: Path, on_ask_human: str) -> Orchestrator:
    cfg = _real_cfg(on_ask_human)
    registry = build_registry(cfg)
    adapter = StubAdapter(
        {"explorer": ok("ok"), "developer": fail("developer always fails")}
    )
    pm = PlanManager(repo, session_id="ws5-real-init")
    await pm.init_plan(_mk_plan())
    return Orchestrator(
        cwd=repo,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="ws5-real-exec",
    )


@pytest.mark.asyncio
async def test_real_loop_block_mode_reaches_terminal_block(tmp_path: Path) -> None:
    """REGRESSION PIN (real loop): with the default ``block`` mode a failing
    developer drives the real loop to a terminal ``blocked`` with NO
    best-effort-commit op — behaviour is unchanged from today."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    orch = await _make_real_orch(repo, on_ask_human="block")

    await ep.run_execute_phase(orch)

    plan = await orch.plan_manager.load()
    assert plan is not None
    statuses = [t.status for t in plan.phases[0].tasks]
    assert all(s in ("blocked", "skipped") for s in statuses), statuses
    assert "best_effort_committed_on_ask_human" not in _ops(repo)


@pytest.mark.asyncio
async def test_real_loop_fail_mode_raises_from_run_execute_phase(
    tmp_path: Path,
) -> None:
    """``fail`` mode exits loudly: the failing-developer real loop raises
    AskHumanDeadEndError all the way out of ``run_execute_phase`` instead of
    silently blocking."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    orch = await _make_real_orch(repo, on_ask_human="fail")

    with pytest.raises(AskHumanDeadEndError):
        await ep.run_execute_phase(orch)


def _dev_writes_partial_then_fails(inv: Any) -> Any:
    """Developer double that WRITES a real file into the per-task worktree
    (``inv.cwd``) but reports failure — the "left partial work in the tree but
    never converged" shape. The written file makes ``get_diff_vs_base`` non-empty
    at the point the worker_exception ladder reaches ``ask_human``, so
    best_effort_commit has a REAL, git-appliable diff to land on main."""
    (Path(inv.cwd) / "feature.py").write_text("def feature():\n    return 42\n")
    return fail(
        "developer left partial work but did not converge",
        subtype="error_max_turns",
    )


@pytest.mark.asyncio
async def test_real_loop_best_effort_commit_completes_end_to_end(
    tmp_path: Path,
) -> None:
    """END-TO-END (real git): a developer that leaves an appliable worktree diff
    but never converges drives the worker_exception ladder to ``ask_human``;
    under ``best_effort_commit`` the orchestrator does a REAL ``git apply`` of the
    worktree diff to main and the task survives to ``complete`` — stamped
    ``needs_human_review`` — instead of silently blocking. The single
    structurally-novel path (real diff @ ask_human → real apply → completion)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    cfg = _real_cfg("best_effort_commit")
    registry = build_registry(cfg)
    adapter = StubAdapter(
        {"explorer": ok("ok"), "developer": _dev_writes_partial_then_fails}
    )
    pm = PlanManager(repo, session_id="ws5-be-init")
    await pm.init_plan(_mk_plan())
    orch = Orchestrator(
        cwd=repo,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="ws5-be-exec",
    )

    await ep.run_execute_phase(orch)

    plan = await orch.plan_manager.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    # The task survived to complete via best-effort commit (NOT blocked).
    assert task.status == "complete", task.status
    assert task.metadata.get("needs_human_review") is True
    # The distinct terminal-category ledger op was recorded.
    assert "best_effort_committed_on_ask_human" in _ops(repo)
    # The worktree diff ACTUALLY landed on main (real git apply + commit).
    committed = subprocess.run(
        ["git", "show", "HEAD:feature.py"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    assert committed.returncode == 0, "feature.py was not committed to main"
    assert "def feature" in committed.stdout
