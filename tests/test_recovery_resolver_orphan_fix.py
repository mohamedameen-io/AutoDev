"""Regression suite for the resolver-orphaning fix (``PhaseStuckError`` at 33%
prevalence in the 30-instance benchmark pilot).

Root cause: several blocker-resolution paths recover a task via
``_resolver_retry`` (re-enable to ``in_progress``) but the caller then returns
that non-terminal task OUT of ``_execute_one``'s retry loop. The DAG dispatcher
(``PlanManager.next_pending_tasks``) only ever selects ``status == "pending"``
tasks, so an ``in_progress`` recovered task is invisible to it — when it is the
last non-terminal task in a phase the phase raises the pre-existing
``PhaseStuckError``.

Two independent orphaning paths, two fixes (both in ``execute_phase.py``):

* **Fix 1** — ``_resolver_retry`` now clears ``escalated`` (a stale
  ``mark_escalated()`` stamp from the ``SOFT_BLOCKER`` / retry-exhaustion /
  ``architect-infra`` rungs) so the caller's ``if task.escalated: return task``
  guard does not orphan a successfully-recovered task.
* **Fix 2** — the 9 direct ``block_task(...)`` sites in ``_execute_one`` that
  used to ``return task`` unconditionally now check ``task.status`` first: when
  ``block_task``'s resolver-first consult RECOVERED the task (status !=
  ``"blocked"``) they ``continue`` the loop (re-dispatch in the SAME call)
  instead of returning a dispatcher-invisible task.

The tests below prove each fix in isolation (Test 1) and end-to-end through the
REAL ``run_execute_phase`` (Tests 2-4). Each e2e test is NON-VACUOUS: without
its fix the run wedges with ``PhaseStuckError`` (so the test would fail).
"""

from __future__ import annotations

import datetime as _dt
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agents import build_registry
from config.defaults import default_config
from errors import GuardrailExceededError
from orchestrator import Orchestrator
from orchestrator import execute_phase as ep
from state import ledger as ledger_mod
from state.plan_manager import PlanManager
from state.schemas import AcceptanceCriterion, Phase, Plan, Task

from stub_adapter import StubAdapter, fail, ok

# Every test here needs the Universal Blocker Resolver ENGAGED (the suite
# default is ``AUTODEV_RESOLVER_DISABLED=1``; this marker clears it via conftest).
pytestmark = pytest.mark.resolver_enabled

_TERMINAL = frozenset({"complete", "blocked", "skipped"})


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan(n_tasks: int = 1) -> Plan:
    tasks = [
        Task(
            id=f"1.{i}",
            phase_id="1",
            title=f"t{i}",
            description=f"d{i}",
            complexity="medium",
        )
        for i in range(1, n_tasks + 1)
    ]
    return Plan(
        plan_id="p-orphan-fix",
        spec_hash="0123456789abcdef",
        phases=[
            Phase(
                id="1",
                title="Setup",
                tasks=tasks,
                acceptance=[AcceptanceCriterion(id="ph-1", description="ok")],
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
        complexity="medium",
    )


def _git_init(repo: Path) -> None:
    """Init a real git repo — makes ``run_execute_phase`` take the production
    per-task worktree path rather than the bare-dir fallback."""
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True)
    (repo / "math_utils.py").write_text("def add(a, b):\n    return a + b\n")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(repo), check=True)


def _make_cfg() -> Any:
    """Deterministic config: tournaments off, retries fast + shallow, and the
    resolver's per-blocker cycle cap set to 1 so a recovered-but-still-failing
    task reaches a bounded terminal quickly (recover once, then block)."""
    cfg = default_config()
    cfg.tournaments.phase_review.enabled = False
    cfg.tournaments.impl.enabled = False
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.auto_disable_for_models = []
    cfg.tournaments.execute_max_parallel_tasks = 1
    cfg.qa_retry_min_interval_s = 0.0
    cfg.qa_retry_limit = 1
    # Recover-once-then-block: keeps the e2e loops short and deterministic.
    cfg.resolver.max_cycles_per_blocker = 1
    # Disable the real-subprocess QA gates. Test 4 drives a SUCCESSFUL developer
    # diff to the worktree-apply step; the post-code QA gate would otherwise
    # spawn a real ``pytest`` (from PATH) against the worktree, which is (a) not
    # what these tests exercise and (b) non-deterministic in a shared env. Tests
    # 2/3 never reach the gate (their developer never produces a usable diff), so
    # this is a no-op for them.
    cfg.qa_gates.syntax_check = False
    cfg.qa_gates.lint = False
    cfg.qa_gates.build_check = False
    cfg.qa_gates.test_runner = False
    cfg.qa_gates.secretscan = False
    return cfg


def _resolution_actions(cwd: Path) -> list[str | None]:
    return [
        e.payload.get("action")
        for e in ledger_mod.read_entries(cwd)
        if e.op == "resolution_chosen"
    ]


# ---------------------------------------------------------------------------
# Test 1 (Fix 1, UNIT): ``_resolver_retry`` clears a stale ``escalated`` stamp
# and the clear survives a cold reload.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolver_retry_clears_stale_escalated(tmp_path: Path) -> None:
    """A task marked ``escalated`` (as the SOFT_BLOCKER / retry-exhaustion /
    architect-infra rungs do via ``mark_escalated`` BEFORE consulting the
    resolver) must come back from ``_resolver_retry`` with ``escalated=False``
    and ``status=="in_progress"`` — otherwise the caller's
    ``if task.escalated: return task`` guard orphans a recovered task.

    Mirrors ``test_recovery_step5_resolver_note_roundtrip``'s reload pattern:
    the clear must round-trip onto a FRESH ``PlanManager``, not just the
    in-memory task returned."""
    cfg = default_config()
    registry = build_registry(cfg)
    pm = PlanManager(tmp_path, session_id="fix1-init")
    await pm.init_plan(_mk_plan())
    orch = Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=StubAdapter({"explorer": ok("ok")}),
        registry=registry,
        session_id="fix1-exec",
    )

    plan = await orch.plan_manager.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    task = await orch.plan_manager.update_task_status(task.id, "in_progress")

    # Reproduce the exact pre-condition the escalating rungs create.
    await orch.plan_manager.mark_escalated(task.id)
    escalated_task = await orch.plan_manager.get_task(task.id)
    assert escalated_task is not None and escalated_task.escalated is True, (
        "precondition failed: mark_escalated did not set escalated=True"
    )

    refreshed = await ep._resolver_retry(
        orch, escalated_task, note="retry_with_changes: try again"
    )
    assert refreshed is not None
    assert refreshed.escalated is False, (
        "Fix 1 regression: _resolver_retry left a stale escalated=True stamp"
    )
    assert refreshed.status == "in_progress", refreshed.status

    # The clear must survive a cold reload (a fresh PlanManager on the same dir).
    pm2 = PlanManager(tmp_path, session_id="fix1-reload")
    reloaded = await pm2.load()
    assert reloaded is not None
    rtask = reloaded.phases[0].tasks[0]
    assert rtask.escalated is False, (
        f"escalated=False clear did not survive reload: escalated={rtask.escalated}"
    )


# ---------------------------------------------------------------------------
# Test 2 (Fix 2, E2E): GUARDRAIL_EXCEEDED at the developer site. This failure
# class NEVER touches ``escalated`` — so Fix 1 alone would NOT make this pass;
# it specifically proves the Fix-2 ``continue`` guard.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guardrail_exceeded_recovery_not_orphaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A developer dispatch that raises ``GuardrailExceededError`` is routed
    through ``block_task`` (site 1); the resolver recovers it via
    ``escalate_budget`` (re-enable to ``in_progress``). Pre-fix that recovered
    task was RETURNED unconditionally → orphaned → ``PhaseStuckError``. Post-fix
    the loop ``continue``s and the phase reaches a clean bounded terminal
    (``blocked`` is correct — the developer never yields a usable diff).

    Two tasks (mirrors the pilot's ``['1.1', '1.2']`` symptom)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    cfg = _make_cfg()
    registry = build_registry(cfg)
    pm = PlanManager(repo, session_id="fix2-guard-init")
    await pm.init_plan(_mk_plan(n_tasks=2))
    orch = Orchestrator(
        cwd=repo,
        cfg=cfg,
        adapter=StubAdapter({"explorer": ok("ok")}),
        registry=registry,
        session_id="fix2-guard-exec",
    )

    async def _fake_delegate(orch_: Any, role: str, env: Any, **kw: Any) -> Any:
        if role == "developer":
            raise GuardrailExceededError("developer turn/decision budget exhausted")
        return ok(f"[fake:{role}] ok")

    monkeypatch.setattr(ep, "delegate", _fake_delegate)

    # Must NOT raise PhaseStuckError — this is the wedge the fix prevents.
    await ep.run_execute_phase(orch)

    plan = await orch.plan_manager.load()
    assert plan is not None
    statuses = [t.status for t in plan.phases[0].tasks]
    assert all(s in _TERMINAL for s in statuses), (
        f"tasks left non-terminal (orphan wedge): {statuses}"
    )

    actions = _resolution_actions(repo)
    assert "escalate_budget" in actions, (
        "resolver did not run its guardrail escalate_budget rung — the test is "
        f"not exercising the recovered-then-continue path (actions={actions})"
    )


# ---------------------------------------------------------------------------
# Test 3 (Fix 1, E2E): SOFT_BLOCKER → consult_knowledge in the FULL pipeline.
# The SOFT_BLOCKER rung calls ``mark_escalated`` BEFORE the resolver; the
# resolver recovers via consult_knowledge → ``_resolver_retry`` must clear
# ``escalated`` so the caller's ``if task.escalated`` guard doesn't orphan it.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_soft_blocker_recovery_clears_escalated_no_wedge(
    tmp_path: Path,
) -> None:
    """Drive ``run_execute_phase`` into the SOFT_BLOCKER escalation rung and
    assert (a) no ``PhaseStuckError``, (b) terminal completion, and (c) the
    ``escalated`` stamp set by ``mark_escalated`` is CLEARED by the resolver's
    ``_resolver_retry`` recovery (Fix 1) — the regression lock for Fix 1 in the
    full pipeline, distinct from the isolated unit test.

    Reaching SOFT_BLOCKER: pre-seed the architect-consult counter (threshold 1
    → ``next_step`` returns ``SOFT_BLOCKER``) and stub ``critic_sounding_board``
    to emit ``RESOLUTION: soft-blocker`` (the anchored directive
    ``_parse_stuck_resolution`` reads)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    cfg = _make_cfg()
    registry = build_registry(cfg)
    adapter = StubAdapter(
        {
            "explorer": ok("ok"),
            "developer": fail("developer always fails"),
            "critic_sounding_board": ok(
                "Analysis: repeated failures indicate a human decision is needed.\n"
                "RESOLUTION: soft-blocker\n"
            ),
        }
    )
    pm = PlanManager(repo, session_id="fix1-soft-init")
    await pm.init_plan(_mk_plan())
    orch = Orchestrator(
        cwd=repo,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="fix1-soft-exec",
    )
    # Route the very first ladder consult straight to SOFT_BLOCKER (in-memory,
    # per-PlanManager-instance — must seed the orchestrator's own manager).
    await orch.plan_manager.increment_architect_consult("1.1")

    # No PhaseStuckError tolerance — the wedge IS the pre-fix failure.
    await ep.run_execute_phase(orch)

    plan = await orch.plan_manager.load()
    assert plan is not None
    statuses = [t.status for t in plan.phases[0].tasks]
    assert all(s in _TERMINAL for s in statuses), (
        f"task left non-terminal (orphan wedge): {statuses}"
    )

    # The SOFT_BLOCKER rung's resolver recovery uses consult_knowledge.
    actions = _resolution_actions(repo)
    assert "consult_knowledge" in actions, (
        f"SOFT_BLOCKER→consult_knowledge rung did not fire (actions={actions})"
    )

    # Fix-1 regression lock: mark_escalated stamped escalated=True, and a
    # subsequent _resolver_retry (resolver_action=="retry") cleared it to False.
    entries = ledger_mod.read_entries(repo)
    esc_true_idxs = [
        i
        for i, e in enumerate(entries)
        if e.op == "update_task_status" and e.payload.get("escalated") is True
    ]
    assert esc_true_idxs, (
        "mark_escalated (escalated=True) never recorded — the SOFT_BLOCKER path "
        "was not reached, so the Fix-1 clear is untested"
    )
    clear_idxs = [
        i
        for i, e in enumerate(entries)
        if e.op == "update_task_status"
        and e.payload.get("resolver_action") == "retry"
        and e.payload.get("escalated") is False
    ]
    assert any(ci > esc_true_idxs[0] for ci in clear_idxs), (
        "Fix 1 regression: no _resolver_retry cleared escalated=False AFTER a "
        f"mark_escalated(escalated=True) — the caller's guard would orphan the "
        f"recovered task (escalated_at={esc_true_idxs}, cleared_at={clear_idxs})"
    )


# ---------------------------------------------------------------------------
# Test 4 (Fix 2, E2E, DIFFERENT failure class): TEST_DIAGNOSIS_NO_SIGNAL at the
# test-diagnosis site (site 8). A different Fix-2 class from GUARDRAIL_EXCEEDED.
#
# NB: WORKTREE_APPLY_FAILED (site 9) was the plan's first-choice "different
# class", but it CANNOT orphan: at the apply step the task is at status
# "tournamented", whose only legal FSM transitions are {complete, blocked}
# (task_state.py). ``_resolver_retry``'s tournamented→in_progress re-enable is
# therefore rejected, so block_task always commits "blocked" there and the
# recovered-then-return path never fires. TEST_DIAGNOSIS_NO_SIGNAL fires at
# status "reviewed" (reviewed→in_progress IS legal), so the resolver genuinely
# re-enables the task — a real orphaning site pre-fix.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_test_diagnosis_no_signal_recovery_not_orphaned(
    tmp_path: Path,
) -> None:
    """The developer/reviewer happy path reaches the test-diagnosis step; the
    test runner produces no diagnostic signal (``total==0``, no "no tests"
    phrasing) → ``TEST_DIAGNOSIS_NO_SIGNAL`` → ``block_task`` (site 8). The
    resolver recovers it via ``consult_knowledge`` (re-enable to ``in_progress``,
    a legal transition from ``reviewed``). Pre-fix that recovered task was
    RETURNED unconditionally → orphaned → ``PhaseStuckError``. Post-fix the loop
    ``continue``s and the phase reaches a bounded terminal."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    diff = (
        "diff --git a/feature.py b/feature.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/feature.py\n"
        "@@ -0,0 +1 @@\n"
        "+x = 1\n"
    )
    cfg = _make_cfg()
    registry = build_registry(cfg)
    adapter = StubAdapter(
        {
            "explorer": ok("ok"),
            "developer": ok(
                "implemented feature",
                diff=diff,
                files_changed=[Path("feature.py")],
            ),
            "reviewer": ok("VERDICT: APPROVED\n"),
            # No parseable RESULTS line and no "no tests"/"skipped" phrasing →
            # classify_test_result -> "no_signal" (the catch-all sixth rung).
            "test_engineer": ok("the runner emitted ambiguous, unparseable output"),
        }
    )
    pm = PlanManager(repo, session_id="fix2-nosig-init")
    await pm.init_plan(_mk_plan())
    orch = Orchestrator(
        cwd=repo,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="fix2-nosig-exec",
    )

    # Must NOT raise PhaseStuckError.
    await ep.run_execute_phase(orch)

    plan = await orch.plan_manager.load()
    assert plan is not None
    statuses = [t.status for t in plan.phases[0].tasks]
    assert all(s in _TERMINAL for s in statuses), (
        f"task left non-terminal (orphan wedge): {statuses}"
    )

    # The recovered-then-continue path must have engaged: a resolution was chosen
    # for the test_diagnosis_no_signal blocker (its first rung is consult_knowledge).
    fc_actions = [
        (e.payload.get("failure_class"), e.payload.get("action"))
        for e in ledger_mod.read_entries(repo)
        if e.op == "resolution_chosen"
    ]
    assert any(fc == "test_diagnosis_no_signal" for fc, _ in fc_actions), (
        "resolver never engaged the test_diagnosis_no_signal blocker — the "
        f"recovered-then-continue path is untested (resolutions={fc_actions})"
    )
