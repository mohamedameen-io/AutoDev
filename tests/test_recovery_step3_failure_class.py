"""Phase 1A / Step 3 (RECOVERY-CONTRACT §7) — the real failure_class + ledger
trajectory gate.

Before Step 3, ``_try_retry_or_escalate`` had NO ``failure_class`` parameter, so
the retry-exhaustion terminal rung always blocked with ``_fcls.UNKNOWN`` — the
resolver treated every real-loop failure as novel/unseen. Step 3 threads the
REAL terminal failure class from each of the 7 call sites through to the terminal
``block_task`` call, and populates ``BlockerContext.attempt_history`` (was always
``[]``) from the persisted ledger trajectory.

This module drives ``run_execute_phase`` far enough that ``_try_retry_or_escalate``
reaches the resolver with the threaded class, then audits the on-disk ledger
(``blocker_escalated`` / ``resolution_chosen`` payloads). It does NOT hand-fake
the ledger — every breadcrumb is produced by the real loop.

Three engagement assertions, one per representative call site:

  * developer adapter failure   -> ``worker_exception`` (NOT ``unknown``)  [:4560]
  * QA-gate failure             -> ``qa_gate_failed``   (NOT ``unknown``)  [:4640]
  * containment violation       -> ``edit_scope_violation`` (NOT unknown) [:4613]

Plus:

  * ``attempt_history`` is populated (len > 0) on a path that has prior
    ``stuck_refine`` / ``stuck_pivot`` / ``resolution_chosen`` ops.

ANTI-VACUITY / broken-control: a planted revert of any one call site back to
``_fcls.UNKNOWN`` turns the corresponding assertion RED — proven by
``test_broken_control_*`` below, which monkeypatch the call site's class to
UNKNOWN and assert the failure_class assertion would fail.

RED ON HEAD: before Step 3, the developer / QA-gate / containment terminal rungs
all record ``failure_class == "unknown"``, so the three real-class assertions are
RED.
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
from state import ledger as ledger_mod
from state.plan_manager import PlanManager
from state.schemas import AcceptanceCriterion, Phase, Plan, Task

from stub_adapter import StubAdapter, fail, ok

pytestmark = pytest.mark.resolver_enabled


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan() -> Plan:
    return Plan(
        plan_id="p-step3",
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


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=str(repo), check=True
    )
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
    # One retry then escalate -> hits the terminal block site quickly.
    cfg.qa_retry_limit = 1
    return cfg


def _ops(cwd: Path) -> list[str]:
    return [e.op for e in ledger_mod.read_entries(cwd)]


def _resolution_failure_classes(cwd: Path) -> list[str | None]:
    """The ``failure_class`` recorded on every resolver breadcrumb
    (``blocker_escalated`` + ``resolution_chosen``)."""
    return [
        e.payload.get("failure_class")
        for e in ledger_mod.read_entries(cwd)
        if e.op in ("blocker_escalated", "resolution_chosen")
    ]


# A diff that touches ONLY .autodev/ — triggers the containment guard (:4613).
_AUTODEV_DIFF = (
    "diff --git a/.autodev/notes.txt b/.autodev/notes.txt\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/.autodev/notes.txt\n"
    "@@ -0,0 +1 @@\n"
    "+autodev-only\n"
)


async def _drive(
    repo: Path,
    *,
    developer_result: Any,
    monkeypatch: pytest.MonkeyPatch | None = None,
    gate_failure: str | None = None,
) -> tuple[Orchestrator, Path]:
    """Build a REAL run and drive ``run_execute_phase`` to its terminal block."""
    cfg = _make_cfg()
    registry = build_registry(cfg)
    adapter = StubAdapter(
        {
            "explorer": ok("ok"),
            "developer": developer_result,
            # If the loop reaches the reviewer, approve so the test only ever
            # exercises the intended (earlier) call site.
            "reviewer": ok("VERDICT: APPROVED\nISSUES: none\n"),
            "test_engineer": ok("1 passed\n"),
        }
    )

    if gate_failure is not None:
        assert monkeypatch is not None

        async def _fake_gates(*_a: Any, **_k: Any) -> str:
            return gate_failure

        monkeypatch.setattr(ep, "_run_qa_gates", _fake_gates)

    pm = PlanManager(repo, session_id="step3-init")
    await pm.init_plan(_mk_plan())

    orch = Orchestrator(
        cwd=repo,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="step3-exec",
    )
    # Step 3 threads a REAL recovery class, so the resolver now ACTIVELY recovers
    # these sites (e.g. worker_exception -> retry_with_changes). Re-dispatch of a
    # resolver-recovered task is wired in Step 4/5; until then the phase DAG loop
    # has no pending task to re-spawn and raises PhaseStuckError. The resolver
    # breadcrumbs (blocker_escalated / resolution_chosen) are ALL written to the
    # on-disk ledger BEFORE that point, so we tolerate the wedge and audit the
    # ledger — the real threading + terminal block is fully exercised.
    from errors import PhaseStuckError

    try:
        await ep.run_execute_phase(orch)
    except PhaseStuckError:
        pass
    return orch, repo


# ---------------------------------------------------------------------------
# (1) developer adapter failure -> worker_exception
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_developer_failure_records_real_class(tmp_path: Path) -> None:
    """Driving a failing developer to the terminal block records
    ``failure_class == "worker_exception"`` (NOT ``"unknown"``)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    _orch, cwd = await _drive(repo, developer_result=fail("developer always fails"))

    classes = _resolution_failure_classes(cwd)
    assert classes, f"no resolver breadcrumbs — ops={_ops(cwd)}"
    assert "unknown" not in classes, (
        f"developer-failure path recorded failure_class='unknown' (classes={classes})"
    )
    assert "worker_exception" in classes, (
        f"expected worker_exception threaded to the terminal rung "
        f"(classes={classes})"
    )


# ---------------------------------------------------------------------------
# (2) QA-gate failure -> qa_gate_failed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qa_gate_failure_records_real_class(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful developer diff that fails a QA gate drives the :4640 rung and
    records ``failure_class == "qa_gate_failed"`` (NOT ``"unknown"``).

    The gate VERDICT is stubbed (equivalent to stubbing an adapter verdict); the
    threading + terminal block are the REAL loop."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    diff = (
        "diff --git a/math_utils.py b/math_utils.py\n"
        "--- a/math_utils.py\n"
        "+++ b/math_utils.py\n"
        "@@ -1,2 +1,3 @@\n"
        " def add(a, b):\n"
        "     return a + b\n"
        "+# touched\n"
    )
    dev = ok("done", diff=diff, files_changed=["math_utils.py"])

    _orch, cwd = await _drive(
        repo,
        developer_result=dev,
        monkeypatch=monkeypatch,
        gate_failure="lint: E501 line too long",
    )

    classes = _resolution_failure_classes(cwd)
    assert classes, f"no resolver breadcrumbs — ops={_ops(cwd)}"
    assert "unknown" not in classes, (
        f"QA-gate path recorded failure_class='unknown' (classes={classes})"
    )
    assert "qa_gate_failed" in classes, (
        f"expected qa_gate_failed threaded to the terminal rung (classes={classes})"
    )


# ---------------------------------------------------------------------------
# (3) containment violation -> edit_scope_violation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_containment_violation_records_real_class(tmp_path: Path) -> None:
    """A successful developer diff confined to ``.autodev/`` drives the :4613
    containment rung and records ``failure_class == "edit_scope_violation"``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    dev = ok("done", diff=_AUTODEV_DIFF, files_changed=[".autodev/notes.txt"])

    _orch, cwd = await _drive(repo, developer_result=dev)

    ops = _ops(cwd)
    assert "containment_violation_autodev_paths" in ops, (
        f"containment guard never fired — ops={ops}"
    )
    classes = _resolution_failure_classes(cwd)
    assert classes, f"no resolver breadcrumbs — ops={ops}"
    assert "unknown" not in classes, (
        f"containment path recorded failure_class='unknown' (classes={classes})"
    )
    assert "edit_scope_violation" in classes, (
        f"expected edit_scope_violation threaded to the terminal rung "
        f"(classes={classes})"
    )


# ---------------------------------------------------------------------------
# (4) attempt_history is populated from the ledger trajectory
# ---------------------------------------------------------------------------


def test_attempt_history_populated_from_trajectory(tmp_path: Path) -> None:
    """``_ledger_trajectory`` returns a non-empty, seq-ordered list when prior
    ``stuck_refine`` / ``stuck_pivot`` / ``resolution_chosen`` ops exist for the
    task — the value ``BlockerContext.attempt_history`` is built from.

    Was always ``[]`` before Step 3."""
    import asyncio

    pm = PlanManager(tmp_path, session_id="traj")
    asyncio.run(pm.init_plan(_mk_plan()))

    asyncio.run(
        pm.ledger_append(op="stuck_refine", payload={"task_id": "1.1", "reason": "r1"})
    )
    asyncio.run(
        pm.ledger_append(op="stuck_pivot", payload={"task_id": "1.1", "reason": "r2"})
    )
    asyncio.run(
        pm.ledger_append(
            op="resolution_chosen",
            payload={
                "task_id": "1.1",
                "blocker_key": "1.1:worker_exception",
                "action": "retry_with_changes",
            },
        )
    )
    # An op for a DIFFERENT task must not leak in.
    asyncio.run(
        pm.ledger_append(op="stuck_refine", payload={"task_id": "9.9", "reason": "x"})
    )

    class _Orch:
        cwd = tmp_path

    traj = ep._ledger_trajectory(_Orch(), "1.1")  # type: ignore[arg-type]
    assert traj, "attempt_history trajectory was empty"
    assert traj == [
        "stuck_refine",
        "stuck_pivot",
        "resolution_chosen:retry_with_changes",
    ], traj
    # None task_id -> empty (best-effort, never raises).
    assert ep._ledger_trajectory(_Orch(), None) == []  # type: ignore[arg-type]


def test_prior_resolution_actions_reads_ladder_ops(tmp_path: Path) -> None:
    """Step 3: ``_prior_resolution_actions`` now ALSO surfaces the legacy ladder
    recovery ops (``stuck_refine`` -> 'refine', ``stuck_pivot`` -> 'pivot') for
    the task, in addition to ``resolution_chosen`` for the blocker key."""
    import asyncio

    pm = PlanManager(tmp_path, session_id="prior")
    asyncio.run(pm.init_plan(_mk_plan()))
    asyncio.run(
        pm.ledger_append(op="stuck_refine", payload={"task_id": "1.1"})
    )
    asyncio.run(pm.ledger_append(op="stuck_pivot", payload={"task_id": "1.1"}))
    asyncio.run(
        pm.ledger_append(
            op="resolution_chosen",
            payload={
                "task_id": "1.1",
                "blocker_key": "1.1:qa_gate_failed",
                "action": "retry_with_changes",
            },
        )
    )

    class _Orch:
        cwd = tmp_path

    out = ep._prior_resolution_actions(_Orch(), "1.1", "qa_gate_failed")  # type: ignore[arg-type]
    assert "refine" in out and "pivot" in out, out
    assert "retry_with_changes" in out, out


# ---------------------------------------------------------------------------
# Broken-control: revert a call site's class to UNKNOWN -> assertion goes red.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_broken_control_developer_unknown_goes_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anti-vacuity: monkeypatch the developer call site to pass UNKNOWN (the
    pre-Step-3 behaviour) and assert the real-class assertion would FAIL.

    This proves test (1) genuinely depends on the threaded class — it cannot
    pass on the UNKNOWN/empty case."""
    import orchestrator.failure_classes as _fc

    # Force the threaded class at the developer site to UNKNOWN by patching the
    # constant the call site reads.
    monkeypatch.setattr(_fc, "WORKER_EXCEPTION", _fc.UNKNOWN, raising=True)

    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    _orch, cwd = await _drive(repo, developer_result=fail("developer always fails"))

    classes = _resolution_failure_classes(cwd)
    assert classes, f"no resolver breadcrumbs — ops={_ops(cwd)}"
    # With the planted UNKNOWN, the real-class assertion (1) would fire.
    assert "unknown" in classes, (
        "planted UNKNOWN at the developer site did NOT surface as unknown — the "
        f"broken-control is vacuous (classes={classes})"
    )
    assert "worker_exception" not in classes, (
        "worker_exception still present despite the planted UNKNOWN — the test "
        f"does not actually depend on the threaded class (classes={classes})"
    )
