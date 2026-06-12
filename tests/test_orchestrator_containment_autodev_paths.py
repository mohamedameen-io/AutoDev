"""Gap 5 (containment): a developer diff confined to AutoDev's OWN
``.autodev/`` directory is rejected as invalid task output.

The observed derailment (live, 358k-file run): a corrective task ``0.c2``
generated for a failed Phase-0 research task became a task about AutoDev's
own internals — its developer edited ``.autodev/evidence/0-drift-verifier.json``
(AutoDev's internal run state) instead of the target repository's code. That
``.autodev/``-only diff was then accepted as legitimate task work and the task
reached ``complete``.

AutoDev owns ``.autodev/`` in the target repo (evidence / ledger / tournament
/ index DB / debug dumps). A diff confined ENTIRELY to that directory is the
reliable signal for this class of derailment — real task work always touches
at least one path outside AutoDev's own directory. The orchestrator must
reject such a diff and route the task through the regular retry/escalate path
rather than letting it flow to the reviewer (where an APPROVED verdict on a
diff that is a no-op to the target repo could carry it to ``complete``).

Coverage:
  * ``_path_is_autodev_owned`` / ``_diff_confined_to_autodev`` pure helpers.
  * End-to-end: a developer returning an ``.autodev/``-only diff does NOT
    reach ``complete``; the ``containment_violation_autodev_paths`` ledger op
    is emitted.
  * Negatives: a diff touching the TARGET repo (even partially) is real work;
    an empty diff (research task) is never tripped by this guard.
"""

from __future__ import annotations

import datetime as _dt
import typing
from pathlib import Path

import pytest

from adapters.types import AgentResult
from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator.execute_phase import (
    _diff_confined_to_autodev,
    _path_is_autodev_owned,
)
from state.schemas import AcceptanceCriterion, Phase, Plan, Task

from stub_adapter import StubAdapter, ok


# ── helpers ────────────────────────────────────────────────────────────────


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _autodev_only_diff() -> str:
    """A diff that touches ONLY ``.autodev/`` — the live derailment shape."""
    return (
        "diff --git a/.autodev/evidence/0-drift-verifier.json "
        "b/.autodev/evidence/0-drift-verifier.json\n"
        "--- a/.autodev/evidence/0-drift-verifier.json\n"
        "+++ b/.autodev/evidence/0-drift-verifier.json\n"
        "@@ -1 +1 @@\n"
        '-{"passed": false}\n'
        '+{"passed": true}\n'
    )


def _target_diff() -> str:
    """A diff that touches the TARGET repository's code (real work)."""
    return (
        "diff --git a/src/gles/profiler.cpp b/src/gles/profiler.cpp\n"
        "--- a/src/gles/profiler.cpp\n"
        "+++ b/src/gles/profiler.cpp\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )


def _result(diff: str, *, success: bool = True) -> AgentResult:
    return AgentResult(
        text="done", success=success, duration_s=0.01, files_changed=[], diff=diff
    )


def _mk_plan() -> Plan:
    task = Task(
        id="0.c2",
        phase_id="0",
        title="drift_verifier: non-standard verdict 'PASS' treated as NEEDS_REVISION",
        description="(corrective) fix the drift verifier verdict vocabulary",
        files=[],
        acceptance=[AcceptanceCriterion(id="ac-1", description="root cause fixed")],
    )
    return Plan(
        plan_id="p-contain",
        spec_hash="d",
        phases=[Phase(id="0", title="Research", tasks=[task])],
        created_at=_iso(),
        updated_at=_iso(),
    )


async def _make_orch(cwd: Path, adapter: StubAdapter) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    cfg.tournaments.review_tournament_enabled = False
    # Keep escalation-driven tests fast (default 30s backoff would stack).
    cfg.qa_retry_min_interval_s = 0.0
    registry = build_registry(cfg)
    orch = Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-contain",
    )
    await orch.plan_manager.init_plan(_mk_plan())
    return orch


# ── pure helper: _path_is_autodev_owned ─────────────────────────────────────


def test_path_is_autodev_owned_matches_nested() -> None:
    assert _path_is_autodev_owned(".autodev/evidence/0-drift-verifier.json") is True


def test_path_is_autodev_owned_matches_dir_itself() -> None:
    assert _path_is_autodev_owned(".autodev") is True


def test_path_is_autodev_owned_strips_leading_dot_slash() -> None:
    assert _path_is_autodev_owned("./.autodev/evidence/x.json") is True


def test_path_is_autodev_owned_rejects_target_code() -> None:
    assert _path_is_autodev_owned("src/gles/profiler.cpp") is False


def test_path_is_autodev_owned_rejects_sibling_prefix() -> None:
    """A sibling that merely starts with the literal string is NOT owned."""
    assert _path_is_autodev_owned(".autodev-notes/x.md") is False


# ── pure helper: _diff_confined_to_autodev ──────────────────────────────────


def test_diff_confined_to_autodev_true_for_autodev_only() -> None:
    assert _diff_confined_to_autodev(_result(_autodev_only_diff())) is True


def test_diff_confined_to_autodev_false_for_mixed_diff() -> None:
    """A diff that ALSO touches the target repo is real work, not confined."""
    mixed = _autodev_only_diff() + _target_diff()
    assert _diff_confined_to_autodev(_result(mixed)) is False


def test_diff_confined_to_autodev_false_for_target_only() -> None:
    assert _diff_confined_to_autodev(_result(_target_diff())) is False


def test_diff_confined_to_autodev_false_for_empty_diff() -> None:
    """Empty diff (research task) must never trip the guard."""
    assert _diff_confined_to_autodev(_result("")) is False


def test_diff_confined_to_autodev_false_for_none_result() -> None:
    assert _diff_confined_to_autodev(None) is False


# ── ledger op registration ──────────────────────────────────────────────────


def test_containment_op_registered_in_literal() -> None:
    from state.ledger import LedgerOp

    assert "containment_violation_autodev_paths" in typing.get_args(LedgerOp)


def test_containment_op_apply_op_is_noop() -> None:
    """Audit-only: replay must not raise and must not mutate plan state."""
    from state.ledger import LedgerEntry, _apply_op

    entry = LedgerEntry(
        seq=1,
        timestamp=_iso(),
        session_id="s",
        op="containment_violation_autodev_paths",
        payload={"task_id": "0.c2", "files": [".autodev/evidence/x.json"]},
        prev_hash="",
        self_hash="x",
    )
    assert _apply_op(None, entry) is None


# ── end-to-end: the derailment is blocked ───────────────────────────────────


@pytest.mark.asyncio
async def test_autodev_only_diff_does_not_complete(tmp_path: Path) -> None:
    """The live gap: a developer that edits ONLY ``.autodev/`` must NOT reach
    ``complete``. Even with the reviewer defaulting to APPROVED, the diff is
    rejected before the reviewer step, so the task ends up escalated/blocked —
    never ``complete``."""
    # Developer always returns the .autodev/-only diff (so every retry repeats
    # the derailment and the task is driven to escalation rather than looping
    # forever). Reviewer is left to the stub default (APPROVED-ish) to prove
    # the guard fires BEFORE the reviewer could approve the no-op diff.
    adapter = StubAdapter(
        {
            "developer": _result(_autodev_only_diff()),
            "reviewer": ok("VERDICT: APPROVED\n"),
        }
    )
    orch = await _make_orch(tmp_path, adapter)

    tasks = await orch.execute()

    assert len(tasks) == 1
    task = tasks[0]
    assert task.status != "complete", (
        f"containment guard failed: .autodev/-only diff reached "
        f"status={task.status!r}"
    )

    # The containment breadcrumb was emitted at least once.
    from state.ledger import read_entries

    ops = [e.op for e in read_entries(tmp_path)]
    assert "containment_violation_autodev_paths" in ops, (
        f"expected containment_violation_autodev_paths ledger op, got: "
        f"{sorted(set(ops))}"
    )


@pytest.mark.asyncio
async def test_target_diff_is_not_blocked_by_containment(tmp_path: Path) -> None:
    """Control: a developer that edits the TARGET repo is NOT flagged by the
    containment guard (no ``containment_violation_autodev_paths`` op)."""
    adapter = StubAdapter(
        {
            "developer": _result(_target_diff()),
            "reviewer": ok("VERDICT: APPROVED\n"),
        }
    )
    orch = await _make_orch(tmp_path, adapter)

    await orch.execute()

    from state.ledger import read_entries

    ops = [e.op for e in read_entries(tmp_path)]
    assert "containment_violation_autodev_paths" not in ops, (
        f"target-repo diff wrongly flagged as containment violation: "
        f"{sorted(set(ops))}"
    )
