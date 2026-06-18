"""F-2 (field-finding) — phase-scoped non-convergence ceiling on same-class
corrective regeneration.

ROOT CAUSE (confirmed from live field ledgers): hard tasks hit the 40-min execute
timeout via an UNBOUNDED corrective-regeneration loop:

    task -> coded/reviewed/tested -> 3-way merge apply FAILS
         -> blocker_escalated(conflict_3way_failed) -> resolver re_architect
         -> _resolver_corrective mints corrective task 1.cN; original task skipped
    then 1.cN executes -> collides again -> conflict_3way_failed -> mints 1.c(N+1)
         -> ... until the corrective cap (8/phase, 24/plan), by which point > 2400s.

The existing per-(task, failure_class) cycle guard (``_maybe_resolve_blocker``'s
``max_cycles_per_blocker``) is keyed on ``f"{task.id}:{failure_class}"`` — but each
freshly-minted corrective has a NEW id => a FRESH counter => the guard never bounds
the CROSS-corrective-task loop. The ONLY ceiling is the corrective cap, too high to
beat the wall clock.

This module reproduces the loop deterministically through the REAL mint chokepoint
(``_resolver_corrective`` / ``_apply_resolution``), then pins that a PHASE-scoped
ceiling terminates it LOUD-FAST after N=3 same-class non-progressing correctives —
while a corrective that MAKES PROGRESS or a DIFFERENT failure_class does NOT trip it
(legitimate recovery preserved).

Harness style mirrors tests/test_recovery_step5_structural_recovery.py: a real
``Orchestrator`` + ``PlanManager`` on a tmp git repo, ``StubAdapter`` agents, driven
under ``resolver_enabled``. Each loop iteration uses a FRESH ``in_progress`` task to
faithfully model the field loop where every corrective carries a brand-new id (the
exact reason the per-task guard never binds).
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
from orchestrator.blocker_guard import block_task
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


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan() -> Plan:
    return Plan(
        plan_id="p-f2-nonconvergence",
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


async def _build_orch(
    repo: Path, *, session: str, corrective_cap: int | None = None
) -> Orchestrator:
    cfg = _make_cfg()
    if corrective_cap is not None:
        # Raise the corrective cap well above the rounds a test drives so the
        # ONLY possible bound is the phase-scoped non-convergence ceiling under
        # test (the 8-task cap would otherwise mask it — that cap is the blunt
        # ceiling P1 supersedes).
        cfg.max_corrective_tasks_per_phase = corrective_cap
        cfg.max_corrective_tasks_per_plan = corrective_cap
    registry = build_registry(cfg)
    adapter = StubAdapter(
        {"explorer": ok("ok"), "developer": fail("developer always fails")}
    )
    pm = PlanManager(repo, session_id=f"{session}-init")
    await pm.init_plan(_mk_plan())
    orch = Orchestrator(
        cwd=repo,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id=f"{session}-exec",
    )
    # Register the resolver chokepoint as run_execute_phase would (so block_task
    # routes terminal blocks through the resolver even though we drive the
    # mint chokepoint directly rather than via the full execute loop).
    orch.block_hook = ep._maybe_resolve_blocker  # type: ignore[attr-defined]
    return orch


def _re_architect_action() -> ResolutionAction:
    """The structural action the conflict ladder emits (prose-only rationale;
    _apply_resolution synthesizes a bulleted direction => >= 1 corrective task)."""
    return ResolutionAction(
        action="re_architect",
        rationale=(
            "merge conflict could not be resolved mechanically: rethink the task "
            "at component altitude so the patches stop colliding."
        ),
        params={},
    )


async def _add_fresh_corrective_in_progress(
    orch: Orchestrator, n: int
) -> Task:
    """Append a fresh corrective-shaped task (NEW id) and mark it in_progress —
    faithfully modelling the field loop where each minted corrective has a brand
    new id (the exact reason the per-(task,failure_class) guard never binds)."""
    task = Task(
        id=f"1.f{n}",
        phase_id="1",
        title=f"corrective driver {n}",
        description="d",
        files=["src/widget.py", "src/factory.py"],
        complexity="medium",
        assigned_agent="developer",
        metadata={"origin": "phase_review_corrective"},
    )
    await orch.plan_manager.append_corrective_tasks("1", [task])
    return await orch.plan_manager.update_task_status(task.id, "in_progress")


async def _count_correctives(orch: Orchestrator) -> int:
    plan = await orch.plan_manager.load()
    assert plan is not None
    return len(
        [
            t
            for t in plan.phases[0].tasks
            if t.metadata.get("origin") == "phase_review_corrective"
        ]
    )


# ---------------------------------------------------------------------------
# RED→GREEN: the SAME-class loop must terminate LOUD-FAST after N=3.
#
# Each iteration drives a FRESH corrective task (NEW id, modelling the field loop)
# through the REAL terminal chokepoint ``blocker_guard.block_task`` with
# failure_class=conflict_3way_failed. block_task routes through the resolver
# (_maybe_resolve_blocker -> deterministic conflict ladder -> re_architect ->
# _apply_resolution -> _resolver_corrective), which mints a corrective and SKIPS
# the driver — exactly the cross-corrective-task loop. The per-(task,
# failure_class) guard never binds because each driver has a NEW id.
#
# BEFORE the fix the ONLY ceiling is the 8-task corrective cap, so the loop mints
# past N=3 (the 40-min churn) — RED. AFTER the fix the phase-scoped same-class
# counter stops minting at N: the resolver declines, emits a loud
# ``corrective_nonconvergent_ceiling`` op, and block_task commits the SINGLE
# terminal ``blocked`` transition (fail loud, fast) — GREEN.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_class_corrective_loop_is_bounded(tmp_path: Path) -> None:
    repo = tmp_path / "repo-f2"
    repo.mkdir()
    _git_init(repo)
    orch = await _build_orch(repo, session="f2")

    ceiling = int(getattr(orch.cfg.resolver, "max_corrective_cycles_per_phase", 3))
    cap = int(getattr(orch.cfg, "max_corrective_tasks_per_phase", 8))
    assert ceiling < cap, (
        "test premise: the non-convergence ceiling must be SMALLER than the "
        f"corrective cap to be meaningful (ceiling={ceiling}, cap={cap})"
    )

    minted_each_round: list[bool] = []
    blocked_each_round: list[bool] = []
    # Drive more rounds than the ceiling. Pre-fix only the 8-task corrective cap
    # can stop the loop; post-fix the phase-scoped ceiling stops it at N.
    rounds = ceiling + 3
    for n in range(rounds):
        task = await _add_fresh_corrective_in_progress(orch, n)
        before = await _count_correctives(orch)
        # The REAL terminal chokepoint — routes through the resolver.
        result = await block_task(
            orch,
            task,
            failure_class=_fcls.CONFLICT_3WAY_FAILED,
            raw_error="conflict_escalation:3way_failed (synthetic)",
            phase_id="1",
            meta={"blocked_reason": "conflict_escalation:3way_failed (synthetic)"},
        )
        after = await _count_correctives(orch)
        # A round "minted" iff the resolver appended a NEW corrective sub-task
        # (the driver task itself was added by the harness; we measure the delta
        # AROUND the block_task call only).
        minted = after > before
        minted_each_round.append(minted)
        blocked_each_round.append(getattr(result, "status", None) == "blocked")
        # Minting recovers the driver (skipped); bounding terminates it (blocked).
        if minted:
            assert result is not None and result.status == "skipped"

    minted_rounds = sum(1 for m in minted_each_round if m)

    # GREEN assertion: the loop is BOUNDED at the ceiling — at most ``ceiling``
    # rounds mint a fresh same-class corrective; the remaining rounds are refused.
    assert minted_rounds <= ceiling, (
        f"F-2 NOT bounded: {minted_rounds} same-class corrective rounds minted "
        f"(expected <= ceiling={ceiling}). minted_each_round={minted_each_round}"
    )
    # Non-vacuity: the loop ran PAST the ceiling and those extra rounds were
    # refused (not merely never attempted), and the ceiling itself minted (so the
    # bound is the ceiling, not some unrelated early stop).
    assert any(minted_each_round[:ceiling]), (
        f"expected the first rounds to mint; minted_each_round={minted_each_round}"
    )
    assert minted_each_round[ceiling:] and not any(minted_each_round[ceiling:]), (
        f"expected rounds after the ceiling to be REFUSED; "
        f"minted_each_round={minted_each_round}"
    )

    # A LOUD, attributable terminal op was emitted (the fail-loud-fast signal).
    ops = [e.op for e in _ledger_mod.read_entries(repo)]
    assert "corrective_nonconvergent_ceiling" in ops, (
        "expected a loud terminal 'corrective_nonconvergent_ceiling' ledger op "
        f"when the phase-scoped ceiling tripped; ops seen={sorted(set(ops))}"
    )

    # The refused rounds terminated LOUD as a real ``blocked`` transition (the
    # single sanctioned committer fired) — NOT unbounded growth toward the wall.
    assert any(blocked_each_round[ceiling:]), (
        "expected a refused round to terminate the driver task as blocked "
        f"(fail-fast terminal); blocked_each_round={blocked_each_round}"
    )


# ---------------------------------------------------------------------------
# NEGATIVE (preserve legitimate recovery #1): a DIFFERENT failure_class does NOT
# count toward the same-class ceiling — the counter is per-class.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_different_failure_class_does_not_trip_ceiling(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo-f2-diff"
    repo.mkdir()
    _git_init(repo)
    # High corrective cap so the ONLY possible bound is the same-class ceiling.
    orch = await _build_orch(repo, session="f2-diff", corrective_cap=1000)
    ceiling = int(getattr(orch.cfg.resolver, "max_corrective_cycles_per_phase", 3))

    # Alternate two DISTINCT structural classes that both mint (re_architect /
    # re_plan). Drive EACH class ``ceiling - 1`` times, so NEITHER class's own
    # per-class counter reaches the ceiling. The COMBINED round count is
    # ``2 * (ceiling - 1)`` which, for ceiling >= 2, EXCEEDS the ceiling — so a
    # BROKEN impl that shared one counter across classes WOULD trip and emit the
    # loud op. A correct per-class impl mints every round and never trips
    # (non-vacuous proof that distinct classes do not share a counter).
    classes = [_fcls.CONFLICT_3WAY_FAILED, _fcls.DAG_INVALID]
    per_class_rounds = ceiling - 1
    assert per_class_rounds >= 1, "ceiling must be >= 2 for this test to be meaningful"
    rounds = per_class_rounds * len(classes)
    assert rounds > ceiling, (
        "test premise: combined rounds must exceed the ceiling so a SHARED-counter "
        f"bug would trip (rounds={rounds}, ceiling={ceiling})"
    )
    minted_rounds = 0
    for n in range(rounds):
        fclass = classes[n % 2]
        action = "re_architect" if fclass == _fcls.CONFLICT_3WAY_FAILED else "re_plan"
        task = await _add_fresh_corrective_in_progress(orch, n)
        before = await _count_correctives(orch)
        ctx = BlockerContext(failure_class=fclass, task_id=task.id, phase_id="1")
        await ep._apply_resolution(
            orch,
            task,
            ctx,
            ResolutionAction(action=action, rationale="prose", params={}),
        )
        after = await _count_correctives(orch)
        if after > before:
            minted_rounds += 1

    # Per-class counters are independent => every round mints, nothing is bounded.
    assert minted_rounds == rounds, (
        "alternating distinct failure classes were wrongly bounded by the "
        f"same-class ceiling: minted {minted_rounds}, expected {rounds} "
        "(per-class counters must be independent)"
    )
    ops = [e.op for e in _ledger_mod.read_entries(repo)]
    assert "corrective_nonconvergent_ceiling" not in ops, (
        "the non-convergence ceiling fired on DISTINCT alternating classes — it "
        "must only fire on the SAME class recurring without progress"
    )


# ---------------------------------------------------------------------------
# NEGATIVE (preserve legitimate recovery #2): a corrective that MAKES PROGRESS
# (a task in the phase completes) RESETS the same-class counter, so a subsequent
# same-class corrective still mints — multi-corrective recovery proceeds.
# ---------------------------------------------------------------------------


async def _cycle_counter(orch: Orchestrator, failure_class: str) -> int:
    """Read the persisted per-phase same-class corrective-cycle counter."""
    plan = await orch.plan_manager.load()
    assert plan is not None
    key = f"corrective_cycle_count:{failure_class}"
    return int((plan.phases[0].metadata or {}).get(key, 0))


@pytest.mark.asyncio
async def test_progress_resets_ceiling(tmp_path: Path) -> None:
    repo = tmp_path / "repo-f2-progress"
    repo.mkdir()
    _git_init(repo)
    # High corrective cap so the ONLY possible bound is the same-class ceiling.
    orch = await _build_orch(repo, session="f2-progress", corrective_cap=1000)
    ceiling = int(getattr(orch.cfg.resolver, "max_corrective_cycles_per_phase", 3))

    async def _drive_round(n: int) -> bool:
        task = await _add_fresh_corrective_in_progress(orch, n)
        before = await _count_correctives(orch)
        ctx = BlockerContext(
            failure_class=_fcls.CONFLICT_3WAY_FAILED, task_id=task.id, phase_id="1"
        )
        await ep._apply_resolution(orch, task, ctx, _re_architect_action())
        return (await _count_correctives(orch)) > before

    # Run ceiling-1 same-class rounds (all mint; counter approaches but does not
    # reach the ceiling).
    for n in range(ceiling - 1):
        assert await _drive_round(n), f"round {n} should have minted"
    assert await _cycle_counter(orch, _fcls.CONFLICT_3WAY_FAILED) == ceiling - 1, (
        "precondition: the same-class counter should have accumulated"
    )

    # FORWARD PROGRESS: a corrective task completes. ``_reset_corrective_cycle_counters``
    # is the EXACT helper the two task-``complete`` sites call (beside
    # reset_stuck_state) — invoking it here exercises that progress hook. It must
    # zero the same-class counter so the loop's budget is replenished.
    await ep._reset_corrective_cycle_counters(orch, phase_id="1")
    assert await _cycle_counter(orch, _fcls.CONFLICT_3WAY_FAILED) == 0, (
        "forward progress did not reset the same-class corrective-cycle counter"
    )

    # Now run another FULL ``ceiling`` same-class rounds. Because progress reset
    # the counter, ALL of these mint (the loop was NOT prematurely terminated —
    # legitimate multi-corrective recovery proceeds).
    minted_after = 0
    for n in range(ceiling):
        if await _drive_round(1000 + n):
            minted_after += 1
        else:
            break

    assert minted_after == ceiling, (
        "forward progress did not reset the non-convergence counter: only "
        f"{minted_after} same-class correctives minted after a completion "
        f"(expected a full ceiling={ceiling} fresh budget)"
    )
    # And no premature loud-ceiling op fired (recovery preserved, not bounded).
    ops = [e.op for e in _ledger_mod.read_entries(repo)]
    assert "corrective_nonconvergent_ceiling" not in ops, (
        "the ceiling fired despite forward progress resetting the counter"
    )
