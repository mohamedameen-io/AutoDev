"""Step 5 (RECOVERY-CONTRACT §7 Step 5; gate R3) — structural-action recovery.

Step 5 fixes the EMPIRICALLY-CONFIRMED delivery binding constraint
(field-probes/SYNTHESIS.md): the universal execute failure was the cascade
``conflict_3way_failed → resolver re_architect → fell_through → task blocked``,
reproduced on a feature, a refactor, and a 50k-file repo. Root cause: the
resolver's STRUCTURAL actions (``re_architect`` / ``re_plan`` / ``narrow_scope`` /
``split_task``) emitted only a PROSE ``rationale``; ``parse_corrective_direction``
splits on TOP-LEVEL bullets, so prose → 0 corrective tasks → ``_resolver_corrective``
returns ``None`` → the resolver DECLINES → the task hard-blocks.

The fix (``execute_phase._synthesize_corrective_direction`` +
``_apply_resolution``): when the structural action did not supply a STRUCTURED
``params['direction']``, synthesize a bulleted direction from the task context +
action type so the parser yields >= 1 corrective task. The original task is then
``skipped`` (recovered), not ``blocked``.

This module proves R3 for ALL THREE structural classes
(``CONFLICT_3WAY_FAILED``, ``DAG_INVALID``, ``EDIT_SCOPE_VIOLATION``) through the
real resolver path (``deterministic_action`` → ``_apply_resolution`` →
``_resolver_corrective``), driven via ``_try_retry_or_escalate`` under
``resolver_enabled``:

  * the original task ends ``status != "blocked"`` (skipped), and
  * corrective tasks were APPENDED to the phase.

Non-vacuity / broken-control: reverting the synthesis (forcing the PROSE
rationale into the parser) makes ``parse_corrective_direction`` return 0 →
``_resolver_corrective`` returns None → the task would block. We assert that
broken control directly so the gate cannot pass on nothing.
"""

from __future__ import annotations

import datetime as _dt
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator import execute_phase as ep
from orchestrator import failure_classes as _fcls
from orchestrator.corrective_parser import parse_corrective_direction
from orchestrator.worktree import WorktreeManager
from state import ledger as _ledger_mod
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

_STRUCTURAL_CLASSES = (
    _fcls.CONFLICT_3WAY_FAILED,
    _fcls.DAG_INVALID,
    _fcls.EDIT_SCOPE_VIOLATION,
)


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan() -> Plan:
    return Plan(
        plan_id="p-step5-structural",
        spec_hash="0123456789abcdef",
        phases=[
            Phase(
                id="1",
                title="Setup",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="Add the widget factory",
                        description="d1",
                        files=["src/widget.py", "src/factory.py"],
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


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True)
    (repo / "math_utils.py").write_text("def add(a, b):\n    return a + b\n")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(repo), check=True)


def _make_cfg() -> Any:
    cfg = default_config()
    cfg.tournaments.phase_review.enabled = False
    cfg.tournaments.impl.enabled = False
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.auto_disable_for_models = []
    cfg.qa_retry_min_interval_s = 0.0
    cfg.qa_retry_limit = 1
    return cfg


async def _build_orch(repo: Path, *, session: str) -> Orchestrator:
    cfg = _make_cfg()
    registry = build_registry(cfg)
    adapter = StubAdapter(
        {"explorer": ok("ok"), "developer": fail("developer always fails")}
    )
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
# R3: each structural class recovers (skipped, not blocked) + injects tasks.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("failure_class", _STRUCTURAL_CLASSES)
@pytest.mark.asyncio
async def test_structural_class_recovers_via_resolver(
    tmp_path: Path, failure_class: str
) -> None:
    """For each structural class, the resolver injects corrective tasks and the
    original task ends ``status != 'blocked'`` (skipped) — driven through the real
    deterministic-action → _apply_resolution → _resolver_corrective path."""
    repo = tmp_path / f"repo-{failure_class}"
    repo.mkdir()
    _git_init(repo)

    orch = await _build_orch(repo, session=f"step5-{failure_class}")
    plan = await orch.plan_manager.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    task = await orch.plan_manager.update_task_status(task.id, "in_progress")
    base_task_count = len(plan.phases[0].tasks)

    result = await ep._try_retry_or_escalate(
        orch,
        task,
        retry_limit=orch.cfg.qa_retry_limit,
        reason=f"structural failure: {failure_class}",
        failure_class=failure_class,
    )

    # 1) The original task is recovered (skipped), NOT blocked.
    assert getattr(result, "status", None) != "blocked", (
        f"{failure_class}: resolver failed to recover structurally "
        f"(status={getattr(result, 'status', None)})"
    )
    assert getattr(result, "status", None) == "skipped", (
        f"{failure_class}: expected the original task to be skipped after "
        f"corrective injection, got {getattr(result, 'status', None)}"
    )

    # 2) Corrective tasks were APPENDED to the phase.
    plan2 = await orch.plan_manager.load()
    assert plan2 is not None
    phase = plan2.phases[0]
    assert len(phase.tasks) > base_task_count, (
        f"{failure_class}: no corrective tasks were appended "
        f"({len(phase.tasks)} <= {base_task_count})"
    )
    corrective = [
        t for t in phase.tasks if t.metadata.get("origin") == "phase_review_corrective"
    ]
    assert corrective, (
        f"{failure_class}: appended tasks are not corrective-origin: "
        f"{[t.metadata for t in phase.tasks]}"
    )


# ---------------------------------------------------------------------------
# The synthesized direction is itself parseable (>= 1 task) for every action.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "min_tasks"),
    [
        ("re_architect", 1),
        ("re_plan", 1),
        ("narrow_scope", 1),
        ("split_task", 2),
    ],
)
def test_synthesized_direction_parses_to_tasks(
    action: str, min_tasks: int
) -> None:
    """The deterministic synthesis helper must produce a direction
    ``parse_corrective_direction`` turns into >= ``min_tasks`` corrective tasks."""
    task = Task(
        id="1.1",
        phase_id="1",
        title="Add the widget factory",
        description="d",
        files=["src/widget.py", "src/factory.py"],
        complexity="medium",
    )
    direction = ep._synthesize_corrective_direction(
        task, action, rationale="patches keep colliding"
    )
    tasks = parse_corrective_direction(direction, phase_id="1", base_task_count=1)
    assert len(tasks) >= min_tasks, (
        f"{action}: synthesized direction parsed to {len(tasks)} tasks "
        f"(< {min_tasks}); direction was:\n{direction}"
    )


# ---------------------------------------------------------------------------
# NON-VACUITY / broken-control: reverting the synthesis (PROSE direction) makes
# the parser return 0 → _resolver_corrective returns None → the task BLOCKS.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_broken_control_prose_direction_yields_zero_then_blocks(
    tmp_path: Path,
) -> None:
    """Broken-control proof: bypass the synthesis by handing ``_apply_resolution``
    a structural action whose ONLY signal is a prose rationale AND whose
    synthesized fallback we revert. We do this by directly exercising the two
    halves:

      * ``parse_corrective_direction(<prose>)`` returns 0 (the bug), and
      * ``_resolver_corrective(orch, task, <prose>)`` returns None (declines),

    proving the gate is non-vacuous: without the synthesis the structural action
    cannot recover and the caller would fall through to a legacy block.
    """
    # Half 1: prose parses to ZERO tasks (the original inertness).
    prose = (
        "merge conflict could not be resolved mechanically: rethink the task at "
        "component altitude so the patches stop colliding."
    )
    zero = parse_corrective_direction(prose, phase_id="1", base_task_count=1)
    assert zero == [], (
        "broken-control invalid: prose direction unexpectedly parsed to "
        f"{len(zero)} tasks"
    )

    # Half 2: _resolver_corrective with that prose declines (returns None), so a
    # caller relying on it would fall through to the legacy block.
    repo = tmp_path / "repo-control"
    repo.mkdir()
    _git_init(repo)
    orch = await _build_orch(repo, session="step5-control")
    plan = await orch.plan_manager.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    task = await orch.plan_manager.update_task_status(task.id, "in_progress")

    declined = await ep._resolver_corrective(orch, task, prose)
    assert declined is None, (
        "broken-control invalid: _resolver_corrective recovered on PROSE "
        "(the synthesis is supposed to be the ONLY thing that makes it work)"
    )

    # And the SAME task, with a SYNTHESIZED direction, DOES recover — proving the
    # difference is the synthesis, not the harness.
    synth = ep._synthesize_corrective_direction(
        task, "re_architect", rationale=prose
    )
    recovered = await ep._resolver_corrective(orch, task, synth)
    assert recovered is not None and recovered.status == "skipped", (
        "synthesis path failed to recover where prose declined"
    )


# ---------------------------------------------------------------------------
# An LLM-supplied STRUCTURED direction must be preserved (not clobbered by the
# synthesized fallback).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_supplied_structured_direction_is_preserved(
    tmp_path: Path,
) -> None:
    """When ``params['direction']`` already has bullets, ``_apply_resolution`` uses
    it verbatim rather than synthesizing a generic fallback."""
    repo = tmp_path / "repo-supplied"
    repo.mkdir()
    _git_init(repo)
    orch = await _build_orch(repo, session="step5-supplied")
    plan = await orch.plan_manager.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    task = await orch.plan_manager.update_task_status(task.id, "in_progress")
    base = len(plan.phases[0].tasks)

    action = ResolutionAction(
        action="re_architect",
        rationale="prose only",
        params={
            "direction": (
                "- First specific corrective step supplied by the LLM resolver.\n"
                "- Second specific corrective step supplied by the LLM resolver."
            )
        },
    )
    ctx = BlockerContext(
        failure_class=_fcls.CONFLICT_3WAY_FAILED, task_id=task.id, phase_id="1"
    )
    result = await ep._apply_resolution(orch, task, ctx, action)
    assert result is not None and result.status == "skipped"

    plan2 = await orch.plan_manager.load()
    assert plan2 is not None
    corrective = [
        t
        for t in plan2.phases[0].tasks
        if t.metadata.get("origin") == "phase_review_corrective"
    ]
    # The two supplied bullets become two corrective tasks (not the single
    # synthesized re_architect bullet).
    assert len(corrective) == 2, (
        f"supplied structured direction not used verbatim: "
        f"{[t.title for t in corrective]} (base was {base})"
    )


# ---------------------------------------------------------------------------
# A1 (Finding #1) — the corrective-retry "no plan initialized" delivery blocker.
#
# Confirmed mechanism (reproduce-first): a corrective task synthesized by
# ``parse_corrective_direction`` is created with ``files=[]`` (the parser never
# sets ``Task.files``; see corrective_parser.py:99-108 and the
# ``files defaults to []`` Pydantic field). When that corrective task hits a
# 3-way merge conflict, ``_apply_with_conflict_escalation`` calls
# ``worktree_mgr.abort_failed_apply(targets=list(task.files))`` with an EMPTY
# list → ``abort_failed_apply`` falls through to a REPO-WIDE ``git clean -fd``
# (worktree.py:992-997) which DELETES the untracked main-repo ``.autodev/``
# directory (ledger + snapshot). The very next ``block_task`` →
# ``update_task_status("blocked")`` → ``PlanManager._load_sync()`` then reads an
# empty ledger → raises ``PlanConcurrentModificationError("no plan
# initialized; call init_plan first")`` → the field-observed worker_exception /
# ``block_path_plan_uninitialized``.
#
# This is the deterministic repro the field analysis ("not reproducible
# deterministically") was missing: it only fires on the corrective-retry path
# because ONLY corrective tasks carry ``files=[]`` (the architect's original
# tasks declare real files, so their ``abort_failed_apply`` is path-scoped and
# never touches ``.autodev/``).
# ---------------------------------------------------------------------------


async def _build_orch_with_conflicting_worktree(
    repo: Path, *, session: str, task: Task
) -> tuple[Orchestrator, WorktreeManager, Path]:
    """Build a REAL orchestrator + a per-task worktree whose diff genuinely
    CONFLICTS with main HEAD, so the production 3-way-merge-fails path runs the
    REAL ``WorktreeManager.abort_failed_apply``.

    The conflict is real (no mocked git): we edit ``math_utils.py`` line 2 in
    the worktree, then edit the SAME line differently in main and commit, so
    both ``git apply`` and ``git apply --3way`` to main fail.
    """
    cfg = _make_cfg()
    registry = build_registry(cfg)
    # The conflict critic chooses ``rebase-and-retry`` so the 3-way apply path
    # fires (and then fails), driving ``abort_failed_apply``.
    adapter = StubAdapter(
        {
            "explorer": ok("ok"),
            "developer": ok("dev"),
            "critic_sounding_board": ok("RESOLUTION: rebase-and-retry\n"),
        }
    )
    orch = Orchestrator(
        cwd=repo,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id=f"{session}-exec",
    )

    tdir = repo / ".autodev" / "tournaments" / f"conf-{task.id}"
    wm = WorktreeManager(repo, tdir)
    worktree = await wm.create_per_task(task.id, base_ref="HEAD")

    # Worktree edit: change line 2 of math_utils.py → produces a diff.
    (worktree / "math_utils.py").write_text(
        "def add(a, b):\n    return a + b + 1  # worktree change\n"
    )

    # Main edit on the SAME line, committed, so the worktree diff no longer
    # applies cleanly AND the 3-way base context is gone → both applies fail.
    (repo / "math_utils.py").write_text(
        "def add(a, b):\n    return a + b + 2  # main change\n"
    )
    subprocess.run(["git", "add", "math_utils.py"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "commit", "-qm", "main diverges"], cwd=str(repo), check=True
    )

    return orch, wm, worktree


@pytest.mark.asyncio
async def test_corrective_retry_conflict_does_not_wipe_plan(
    tmp_path: Path,
) -> None:
    """A1 RED→GREEN: a corrective task (``files=[]``) whose 3-way merge fails
    must NOT lose its plan to a repo-wide ``git clean -fd``.

    Drives the REAL ``_apply_with_conflict_escalation`` with the REAL
    ``WorktreeManager`` and a genuine merge conflict. The corrective task has
    ``files=[]`` exactly as ``parse_corrective_direction`` produces them.

    BEFORE the fix (RED): ``abort_failed_apply([])`` runs a repo-wide
    ``git clean -fd``, deletes ``.autodev/``, and the terminal ``block_task``
    raises "no plan initialized" — surfacing as a ``block_path_plan_uninitialized``
    ledger breadcrumb (when the ledger dir is recreated by the breadcrumb path)
    or an outright raise. AFTER the fix: the plan ledger SURVIVES, the corrective
    task reaches a real terminal ``blocked`` state, and NO
    ``block_path_plan_uninitialized`` op is emitted.
    """
    repo = tmp_path / "repo-a1"
    repo.mkdir()
    _git_init(repo)

    # Seed a plan whose phase holds the ORIGINAL task plus a CORRECTIVE task
    # (origin=phase_review_corrective, files=[]) — the shape the resolver's
    # conflict→re_architect path appends. We drive the corrective task directly.
    plan = Plan(
        plan_id="p-a1",
        spec_hash="0123456789abcdef",
        phases=[
            Phase(
                id="1",
                title="Setup",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="Add the widget factory",
                        description="d1",
                        files=["math_utils.py"],
                        complexity="medium",
                    ),
                    # Corrective task: NO files (matches parse_corrective_direction).
                    Task(
                        id="1.c2",
                        phase_id="1",
                        title="Re-implement as smaller non-overlapping changes",
                        description="corrective",
                        complexity="medium",
                        assigned_agent="developer",
                        metadata={"origin": "phase_review_corrective"},
                    ),
                ],
                acceptance=[AcceptanceCriterion(id="ph-1", description="ok")],
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
        complexity="medium",
    )
    pm = PlanManager(repo, session_id="a1-init")
    await pm.init_plan(plan)

    ledger_file = repo / ".autodev" / "plan-ledger.jsonl"
    assert ledger_file.exists(), "precondition: plan ledger must exist"

    corrective = (await pm.get_task("1.c2")) or pytest.fail("corrective missing")
    assert corrective.files == [], "repro precondition: corrective has empty files"

    orch, wm, worktree = await _build_orch_with_conflicting_worktree(
        repo, session="a1", task=corrective
    )
    # Drive the corrective task into the conflict-escalation path.
    await orch.plan_manager.update_task_status("1.c2", "in_progress")

    applied = await ep._apply_with_conflict_escalation(
        orch, corrective, worktree, wm
    )

    # The 3-way merge MUST have failed (the conflict is real).
    assert applied is False, "expected the 3-way apply to fail on a real conflict"

    # CORE ASSERTION (the bug): the plan ledger must SURVIVE the conflict-recovery
    # git ops. Pre-fix the repo-wide ``git clean -fd`` deletes it.
    assert ledger_file.exists(), (
        "REGRESSION/BUG: conflict-recovery wiped the main-repo plan ledger "
        "(.autodev/plan-ledger.jsonl) — the corrective-retry 'no plan "
        "initialized' delivery blocker"
    )

    # The corrective task reached a REAL terminal state against a LOADED plan.
    # With the resolver enabled, ``block_task`` routes through the resolver,
    # which recovers the conflict (``re_architect`` → ``skipped`` + a new
    # corrective sub-task) rather than blocking — both ``blocked`` and
    # ``skipped`` are legitimate terminals reached against a LOADED plan. The
    # delivery blocker was the task DYING on "no plan initialized" instead of
    # reaching ANY real terminal; that is what must no longer happen.
    reloaded = await orch.plan_manager.load()
    assert reloaded is not None, "plan unexpectedly None after conflict recovery"
    ct = next(
        (t for t in reloaded.phases[0].tasks if t.id == "1.c2"), None
    )
    assert ct is not None and ct.status in ("blocked", "skipped"), (
        f"corrective task did not reach a real terminal state against a loaded "
        f"plan: {getattr(ct, 'status', None)}"
    )

    # The field signature must be ABSENT: no spurious "no plan initialized".
    ops = [e.op for e in _ledger_mod.read_entries(repo)]
    assert "block_path_plan_uninitialized" not in ops, (
        "the corrective-retry block raised 'no plan initialized' "
        f"(block_path_plan_uninitialized emitted); ledger ops={ops}"
    )
