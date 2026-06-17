"""Step 5 (RECOVERY-CONTRACT §7 Parts 3 & 4) — resolver_note round-trip + the
no-plan-initialized guard.

Part 3 (RECOVER_TASK guidance injection): ``_resolver_retry`` stamps a
``resolver_note`` into the task metadata when it re-enables a task, but pre-Step-5
``update_task_status`` only wrote known Task fields — the note landed in the
ledger payload and was DROPPED from the in-memory Task + snapshot, so the
developer loop could never read it back into ``last_issues``. Step 5 persists the
note onto ``Task.metadata`` (a round-tripping field) and reads it at the developer
loop top.

This module asserts:
  * the note SURVIVES a reload (the round-trip the contract §9.5 flagged), and
  * a clear (``resolver_note=None``) round-trips too (the loop consumes it).

Part 4: the field-observed ``worker_exception: "no plan initialized; call
init_plan first"`` on the conflict→corrective retry path. The exact production
mechanism was NOT reproducible deterministically (the suspected
``abort_failed_apply`` ``git clean -fd`` does NOT delete the gitignored
``.autodev/`` ledger — verified). Step 5 adds a DEFENSIVE GUARD at the terminal
``block_task`` commit: a "no plan initialized" raise now emits an attributable
``block_path_plan_uninitialized`` breadcrumb and re-raises (so genuine corruption
stays loud) instead of silently surfacing as a misclassified worker crash. This
module exercises the guard.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from agents import build_registry
from config.defaults import default_config
from errors import PlanConcurrentModificationError
from orchestrator import Orchestrator
from orchestrator import execute_phase as ep
from orchestrator import failure_classes as _fcls
from orchestrator.blocker_guard import block_task
from state import ledger as ledger_mod
from state.plan_manager import PlanManager
from state.schemas import AcceptanceCriterion, Phase, Plan, Task

from stub_adapter import StubAdapter, ok


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan() -> Plan:
    return Plan(
        plan_id="p-step5-note",
        spec_hash="0123456789abcdef",
        phases=[
            Phase(
                id="1",
                title="Setup",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="t1",
                        description="d1",
                        complexity="medium",
                    ),
                ],
                acceptance=[AcceptanceCriterion(id="ph-1", description="ok")],
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
        complexity="medium",
    )


async def _build_orch(repo: Path, *, session: str) -> Orchestrator:
    cfg = default_config()
    registry = build_registry(cfg)
    adapter = StubAdapter({"explorer": ok("ok")})
    pm = PlanManager(repo, session_id=f"{session}-init")
    await pm.init_plan(_mk_plan())
    return Orchestrator(
        cwd=repo,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id=f"{session}-exec",
    )


# ---------------------------------------------------------------------------
# Part 3: resolver_note round-trips onto Task.metadata and survives reload.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolver_note_survives_reload(tmp_path: Path) -> None:
    """``_resolver_retry`` writes a ``resolver_note``; it must be readable on a
    FRESH PlanManager (reload), not just on the in-memory task it returned."""
    orch = await _build_orch(tmp_path, session="note-survive")
    plan = await orch.plan_manager.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    task = await orch.plan_manager.update_task_status(task.id, "in_progress")

    note = "retry_with_changes: re-run the failing assertion with the diff context"
    refreshed = await ep._resolver_retry(orch, task, note=note)
    assert refreshed is not None
    # The returned task carries the note...
    assert refreshed.metadata.get("resolver_note") == note[:500]

    # ...AND a brand-new PlanManager (cold reload) sees it too — the round-trip
    # the contract §9.5 flagged as missing pre-Step-5.
    pm2 = PlanManager(tmp_path, session_id="note-survive-reload")
    reloaded = await pm2.load()
    assert reloaded is not None
    rtask = reloaded.phases[0].tasks[0]
    assert rtask.metadata.get("resolver_note") == note[:500], (
        f"resolver_note did NOT survive reload: {rtask.metadata}"
    )


@pytest.mark.asyncio
async def test_resolver_note_clear_round_trips(tmp_path: Path) -> None:
    """Clearing the note (``resolver_note=None``) — the developer-loop consume
    step — must also round-trip (the key is removed on reload)."""
    orch = await _build_orch(tmp_path, session="note-clear")
    plan = await orch.plan_manager.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    task = await orch.plan_manager.update_task_status(task.id, "in_progress")
    task = (
        await ep._resolver_retry(orch, task, note="some guidance")
    ) or task
    assert task.metadata.get("resolver_note")

    # Same-status clear (the consume edge: in_progress -> in_progress).
    await orch.plan_manager.update_task_status(
        task.id, "in_progress", meta={"resolver_note": None}
    )
    pm2 = PlanManager(tmp_path, session_id="note-clear-reload")
    reloaded = await pm2.load()
    assert reloaded is not None
    assert "resolver_note" not in reloaded.phases[0].tasks[0].metadata, (
        "resolver_note clear did not round-trip"
    )


@pytest.mark.asyncio
async def test_resolver_note_lands_in_next_developer_last_issues(
    tmp_path: Path,
) -> None:
    """END-TO-END Part 3: after the resolver stamps a note, the NEXT developer
    dispatch sees it. We assert on the captured ``last_issues`` of the developer
    envelope by intercepting ``delegate``."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True)
    (repo / "f.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(repo), check=True)

    orch = await _build_orch(repo, session="note-e2e")
    plan = await orch.plan_manager.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    task = await orch.plan_manager.update_task_status(task.id, "in_progress")

    # Stamp a resolver note (as _resolver_retry would on a RECOVER_TASK action).
    note = "resolver says: narrow the change to f.py only"
    task = (await ep._resolver_retry(orch, task, note=note)) or task

    # Capture last_issues handed to the developer by patching delegate. We raise
    # a sentinel to stop _execute_one right after the first developer dispatch.
    captured: dict[str, object] = {}

    class _Stop(Exception):
        pass

    async def _fake_delegate(orch_, role, env, **kw):  # noqa: ANN001
        captured["last_issues"] = kw.get("last_issues")
        captured["env_prior_issues"] = env.context.get("prior_issues")
        raise _Stop()

    orig = ep.delegate
    ep.delegate = _fake_delegate  # type: ignore[assignment]
    try:
        with pytest.raises(_Stop):
            await ep._execute_one(orch, task)
    finally:
        ep.delegate = orig  # type: ignore[assignment]

    last_issues = captured.get("last_issues") or []
    assert any(note in str(x) for x in last_issues), (
        f"resolver note did not reach the developer's last_issues: {last_issues}"
    )
    prior = captured.get("env_prior_issues") or []
    assert any(note in str(x) for x in prior), (
        f"resolver note did not reach the developer envelope: {prior}"
    )


# ---------------------------------------------------------------------------
# Part 4: the no-plan-initialized guard at the terminal block_task commit.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_block_task_on_uninitialized_plan_breadcrumbs_then_raises(
    tmp_path: Path,
) -> None:
    """When the terminal ``block_task`` commit hits an empty/uninitialized ledger,
    it must (a) emit an attributable ``block_path_plan_uninitialized`` breadcrumb
    and (b) RE-RAISE the "no plan initialized" error — so a genuine state
    corruption stays loud and is no longer misclassified as a fresh
    ``worker_exception`` (the field P5/P6 symptom)."""
    # Build an orchestrator whose PlanManager has NO plan initialized: a bare repo
    # dir with no init_plan call.
    cfg = default_config()
    registry = build_registry(cfg)
    orch = Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=StubAdapter({}),
        registry=registry,
        session_id="uninit-exec",
    )
    # Disable the resolver so block_task goes straight to the terminal commit
    # (the guard is on the commit, not the resolver path).
    import os

    os.environ["AUTODEV_RESOLVER_DISABLED"] = "1"
    try:
        task = Task(id="1.1", phase_id="1", title="t", description="d")
        with pytest.raises(PlanConcurrentModificationError, match="no plan initialized"):
            await block_task(
                orch,
                task,
                failure_class=_fcls.CONFLICT_3WAY_FAILED,
                raw_error="three-way merge could not be resolved",
            )
    finally:
        os.environ.pop("AUTODEV_RESOLVER_DISABLED", None)

    # The attributable breadcrumb must be present so the missing-ledger condition
    # is diagnosable rather than self-masking.
    ops = [e.op for e in ledger_mod.read_entries(tmp_path)]
    assert "block_path_plan_uninitialized" in ops, (
        f"guard did not emit the attributable breadcrumb (ops={ops})"
    )
