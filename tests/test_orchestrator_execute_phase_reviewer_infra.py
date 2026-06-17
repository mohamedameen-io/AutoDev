"""v0.41.0 (Workstream A1): reviewer turn-exhaustion is an INFRA failure,
not a developer-discard.

Background: when the ``reviewer`` agent exhausts its turn budget it returns
an empty / truncated response. :func:`_parse_review_verdict` classifies that
as ``MALFORMED`` — which the caller used to route as a *developer discard +
retry*, looping ``qa_retry_limit`` times until the task was blocked even
though the developer's diff was correct (the Run-3 reviewer-MALFORMED loop).

The fix (caller-side, not parser-side): before treating a MALFORMED verdict
as a developer-discard, check whether the REVIEWER itself ran out of turns
(``result.subtype in _TURN_EXHAUSTION_SUBTYPES``). If so:

1. retry the *reviewer* with an escalated turn budget, and
2. if the reviewer still fails → SOFT-PASS the review (accept the developer's
   diff, stamp APPROVED ReviewEvidence with a ``reviewer_infra_softpass``
   marker) rather than discarding correct work.

A genuinely malformed (non-exhaustion) reviewer response must STILL route to
the existing format-retry / discard path.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adapters.types import AgentResult
from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator import execute_phase as ep
from state.evidence import write_evidence
from state.schemas import (
    AcceptanceCriterion,
    Phase,
    Plan,
    ReviewEvidence,
    Task,
)

from stub_adapter import StubAdapter, fail, ok


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan() -> Plan:
    return Plan(
        plan_id="p-exec-infra",
        spec_hash="d",
        phases=[
            Phase(
                id="1",
                title="Work",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="Add subtract",
                        description="Implement subtract(a, b)",
                        files=["math.py"],
                        acceptance=[
                            AcceptanceCriterion(
                                id="ac-1", description="tests pass"
                            ),
                        ],
                    ),
                ],
            )
        ],
        created_at=_iso(),
        updated_at=_iso(),
    )


async def _make_orch(cwd: Path, adapter: StubAdapter) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    cfg.tournaments.review_tournament_enabled = False
    registry = build_registry(cfg)
    orch = Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-exec-infra",
    )
    await orch.plan_manager.init_plan(_mk_plan())
    return orch


def _coder_ok_with_diff() -> AgentResult:
    return AgentResult(
        success=True,
        text="wrote subtract",
        diff=(
            "diff --git a/math.py b/math.py\n"
            "--- a/math.py\n"
            "+++ b/math.py\n"
            "@@ -0,0 +1 @@\n"
            "+def subtract(a,b): return a-b\n"
        ),
        files_changed=[Path("math.py")],
        duration_s=0.1,
    )


def _reviewer_exhausted() -> AgentResult:
    """A reviewer that ran out of turns: empty text + ``error_max_turns``."""
    return AgentResult(
        success=False,
        text="",
        duration_s=0.01,
        error="agent exhausted its turn budget",
        subtype="error_max_turns",
    )


def _test_engineer_ok() -> AgentResult:
    return ok("ran pytest\nRESULTS: passed=3 failed=0 total=3")


def _read_review_evidence(cwd: Path) -> dict:
    path = cwd / ".autodev" / "evidence" / "1.1-review.json"
    assert path.exists(), "expected review evidence to be written"
    return json.loads(path.read_text())


@pytest.mark.asyncio
async def test_reviewer_exhaustion_retries_then_soft_passes(
    tmp_path: Path,
) -> None:
    """Reviewer ``error_max_turns`` → retry reviewer → SOFT-PASS (accept diff).

    The developer diff is correct; the reviewer keeps exhausting its budget.
    The task must COMPLETE (developer diff accepted), NOT be discarded /
    blocked, and the reviewer must have been retried (called > 1).
    """
    adapter = StubAdapter(
        {
            # A single correct developer diff — it must NOT be discarded /
            # re-run as if it were a bad diff.
            "developer": _coder_ok_with_diff(),
            # Reviewer exhausts turns on every call (initial + escalated retry).
            "reviewer": [
                _reviewer_exhausted(),
                _reviewer_exhausted(),
                _reviewer_exhausted(),
            ],
            "test_engineer": _test_engineer_ok(),
        }
    )
    orch = await _make_orch(tmp_path, adapter)
    tasks = await orch.execute()

    assert len(tasks) == 1
    task = tasks[0]
    # SOFT-PASS: the task completed on the (correct) developer diff.
    assert task.status == "complete", (
        f"expected complete (soft-pass), got {task.status!r} "
        f"(escalated={task.escalated})"
    )
    assert task.escalated is False
    # The developer was NOT re-run as a discard — exactly one developer call.
    assert adapter.count("developer") == 1
    # The reviewer was retried at least once (initial exhaustion + escalated).
    assert adapter.count("reviewer") >= 2
    # test_engineer ran (we fell through to the test step after soft-pass).
    assert adapter.count("test_engineer") == 1

    # The stamped review evidence records the soft-pass: APPROVED verdict
    # carrying the ``reviewer_infra_softpass`` marker in the issues list.
    ev = _read_review_evidence(tmp_path)
    assert ev["verdict"] == "APPROVED"
    assert any(
        "reviewer_infra_softpass" in issue for issue in ev.get("issues", [])
    ), f"soft-pass marker missing from issues: {ev.get('issues')!r}"


@pytest.mark.asyncio
async def test_reviewer_exhaustion_then_recovers_uses_real_verdict(
    tmp_path: Path,
) -> None:
    """If the escalated reviewer retry produces a real verdict, use it.

    The first reviewer call exhausts turns (MALFORMED-infra); the escalated
    retry returns a clean APPROVED. The task completes on that real verdict
    (no soft-pass marker needed) and the developer diff is never discarded.
    """
    adapter = StubAdapter(
        {
            "developer": _coder_ok_with_diff(),
            "reviewer": [
                _reviewer_exhausted(),
                ok("VERDICT: APPROVED\n- looks correct"),
            ],
            "test_engineer": _test_engineer_ok(),
        }
    )
    orch = await _make_orch(tmp_path, adapter)
    tasks = await orch.execute()

    task = tasks[0]
    assert task.status == "complete"
    assert task.escalated is False
    assert adapter.count("developer") == 1
    assert adapter.count("reviewer") == 2

    ev = _read_review_evidence(tmp_path)
    assert ev["verdict"] == "APPROVED"
    # A genuine recovered verdict carries NO soft-pass marker.
    assert not any(
        "reviewer_infra_softpass" in issue for issue in ev.get("issues", [])
    )


@pytest.mark.asyncio
async def test_genuine_malformed_non_exhaustion_routes_to_format_retry(
    tmp_path: Path,
) -> None:
    """A non-exhaustion MALFORMED response still routes to format-retry.

    Here the reviewer SUCCEEDS (subtype != error_max_turns) but emits prose
    with no parseable verdict → MALFORMED. This is a genuine
    format/content failure (NOT infra), so it must go through the existing
    discard+retry path — NOT the soft-pass path. After the retry yields a
    clean APPROVED, the task completes via the normal route (the developer
    diff was re-run as a discard).
    """
    malformed_prose = ok(
        "I looked at the diff and I have some thoughts but I forgot to "
        "emit a verdict line."
    )
    # Sanity: this parses to MALFORMED and carries a non-exhaustion subtype.
    assert malformed_prose.subtype != "error_max_turns"

    adapter = StubAdapter(
        {
            # Two developer calls: the format-retry path discards + re-runs.
            "developer": [_coder_ok_with_diff(), _coder_ok_with_diff()],
            "reviewer": [malformed_prose, ok("VERDICT: APPROVED\n- good")],
            "test_engineer": _test_engineer_ok(),
        }
    )
    orch = await _make_orch(tmp_path, adapter)
    tasks = await orch.execute()

    task = tasks[0]
    assert task.status == "complete"
    # Format-retry path was taken: the developer was re-run (discard+retry),
    # which is the OPPOSITE of the soft-pass infra path (single developer).
    assert adapter.count("developer") == 2
    assert task.retry_count == 1
    assert adapter.count("reviewer") == 2

    # Final review evidence is the recovered APPROVED with NO soft-pass marker.
    ev = _read_review_evidence(tmp_path)
    assert ev["verdict"] == "APPROVED"
    assert not any(
        "reviewer_infra_softpass" in issue for issue in ev.get("issues", [])
    )


# ---------------------------------------------------------------------------
# A4: worktree diff is applied before completion on approved-but-exhausted path
# ---------------------------------------------------------------------------


def _mk_plan_r7() -> Plan:
    return Plan(
        plan_id="p-exec-infra-r7",
        spec_hash="d",
        phases=[
            Phase(
                id="0",
                title="Research",
                tasks=[
                    Task(
                        id="0.1",
                        phase_id="0",
                        title="Confirm something",
                        description="Investigate.",
                        files=["math.py"],
                        acceptance=[
                            AcceptanceCriterion(
                                id="ac-1", description="findings confirmed"
                            ),
                        ],
                    ),
                ],
            )
        ],
        created_at=_mk_plan().created_at,
        updated_at=_mk_plan().updated_at,
    )


async def _make_orch_r7(cwd: Path) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    cfg.tournaments.review_tournament_enabled = False
    registry = build_registry(cfg)
    adapter = StubAdapter({})
    orch = Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-exec-infra-r7",
    )
    await orch.plan_manager.init_plan(_mk_plan_r7())
    return orch


@pytest.mark.asyncio
async def test_approved_exhausted_worktree_diff_applied_to_main(
    tmp_path: Path,
) -> None:
    """APPROVED+exhausted: worktree diff from a prior attempt must be applied.

    Scenario:
      1. A prior developer attempt succeeded and left changes in the worktree
         (simulated: APPROVED ReviewEvidence already on disk, non-empty diff
         in the worktree).
      2. The current (turn-exhausted) developer attempt produced no diff.
      3. _maybe_accept_approved_on_exhaustion fires.
      4. The function MUST call _apply_with_conflict_escalation to flush the
         worktree's prior diff to main before marking the task complete.

    Bug (pre-fix): worktree diff was silently discarded.
    Fix: apply before complete when worktree holds uncommitted changes.
    """
    task_id = "0.1"

    # Seed a genuine APPROVED verdict on disk.
    await write_evidence(
        tmp_path,
        task_id,
        ReviewEvidence(
            task_id=task_id,
            verdict="APPROVED",
            issues=[],
            output_text="VERDICT: APPROVED",
            raw_response="VERDICT: APPROVED",
        ),
    )

    orch = await _make_orch_r7(tmp_path)
    await orch.plan_manager.update_task_status(task_id, "in_progress")
    task = await orch.plan_manager.get_task(task_id)
    assert task is not None

    # The current developer attempt is turn-exhausted and produced no diff.
    exhausted_result = fail(
        "budget escalation exhausted",
        subtype="error_max_turns_escalation_exhausted",
    )

    # Build a mock WorktreeManager whose get_diff_vs_base returns a non-empty
    # diff (simulating a prior successful attempt's uncommitted changes).
    mock_wt_mgr = MagicMock()
    mock_wt_mgr.get_diff_vs_base = AsyncMock(
        return_value=(
            "diff --git a/math.py b/math.py\n"
            "--- a/math.py\n"
            "+++ b/math.py\n"
            "@@ -0,0 +1 @@\n"
            "+def subtract(a, b): return a - b\n"
        )
    )
    mock_worktree = tmp_path / "worktree"

    # Patch _apply_with_conflict_escalation at the module level so we can
    # assert it was called (the real one requires a live git repo).
    apply_called: list[bool] = []

    async def _fake_apply(
        orch_arg, task_arg, wt_arg, wt_mgr_arg
    ):  # type: ignore[return]
        apply_called.append(True)
        return True  # applied successfully

    with patch.object(ep, "_apply_with_conflict_escalation", _fake_apply):
        result = await ep._maybe_accept_approved_on_exhaustion(
            orch,
            task,
            exhausted_result,
            worktree=mock_worktree,
            worktree_mgr=mock_wt_mgr,
        )

    # The task must have completed.
    assert result is not None, "expected completed task, got None"
    assert result.status == "complete", (
        f"expected complete, got {result.status!r}"
    )

    # The apply must have been called to flush the prior diff.
    assert apply_called, (
        "_apply_with_conflict_escalation was NOT called — "
        "prior worktree diff was silently discarded (bug A4)"
    )

    # get_diff_vs_base was called to check the worktree state.
    mock_wt_mgr.get_diff_vs_base.assert_awaited_once_with(mock_worktree)


@pytest.mark.asyncio
async def test_approved_exhausted_empty_worktree_skips_apply(
    tmp_path: Path,
) -> None:
    """When the worktree has NO uncommitted changes, apply is skipped.

    This covers the common case: a research task whose correct output is an
    empty diff (no files changed). The worktree is clean, so _apply_with_
    conflict_escalation should NOT be called — the existing no-op completion
    path is correct.
    """
    task_id = "0.1"

    await write_evidence(
        tmp_path,
        task_id,
        ReviewEvidence(
            task_id=task_id,
            verdict="APPROVED",
            issues=[],
            output_text="VERDICT: APPROVED",
            raw_response="VERDICT: APPROVED",
        ),
    )

    orch = await _make_orch_r7(tmp_path)
    await orch.plan_manager.update_task_status(task_id, "in_progress")
    task = await orch.plan_manager.get_task(task_id)
    assert task is not None

    exhausted_result = fail(
        "budget escalation exhausted",
        subtype="error_max_turns_escalation_exhausted",
    )

    # Worktree is clean: get_diff_vs_base returns empty string.
    mock_wt_mgr = MagicMock()
    mock_wt_mgr.get_diff_vs_base = AsyncMock(return_value="")
    mock_worktree = tmp_path / "worktree"

    apply_called: list[bool] = []

    async def _fake_apply(orch_arg, task_arg, wt_arg, wt_mgr_arg):  # type: ignore[return]
        apply_called.append(True)
        return True

    with patch.object(ep, "_apply_with_conflict_escalation", _fake_apply):
        result = await ep._maybe_accept_approved_on_exhaustion(
            orch,
            task,
            exhausted_result,
            worktree=mock_worktree,
            worktree_mgr=mock_wt_mgr,
        )

    assert result is not None
    assert result.status == "complete"

    # Apply must NOT have been called for a clean worktree.
    assert not apply_called, (
        "_apply_with_conflict_escalation was called despite empty worktree diff"
    )


@pytest.mark.asyncio
async def test_approved_exhausted_diff_check_raises_blocks_task(
    tmp_path: Path,
) -> None:
    """When get_diff_vs_base RAISES, the task must be BLOCKED (not completed).

    Rationale: if we cannot determine whether there is an unapplied diff in
    the worktree, silently completing the task risks discarding approved
    changes — the exact class of silent loss A4 exists to prevent. The safe
    response is to block loud so a human can inspect.

    This test pins: exception from get_diff_vs_base → task.status == "blocked"
    (NOT "complete").
    """
    task_id = "0.1"

    # Seed a genuine APPROVED verdict on disk (required for the gate to fire).
    await write_evidence(
        tmp_path,
        task_id,
        ReviewEvidence(
            task_id=task_id,
            verdict="APPROVED",
            issues=[],
            output_text="VERDICT: APPROVED",
            raw_response="VERDICT: APPROVED",
        ),
    )

    orch = await _make_orch_r7(tmp_path)
    await orch.plan_manager.update_task_status(task_id, "in_progress")
    task = await orch.plan_manager.get_task(task_id)
    assert task is not None

    # The current developer attempt is turn-exhausted and produced no diff.
    exhausted_result = fail(
        "budget escalation exhausted",
        subtype="error_max_turns_escalation_exhausted",
    )

    # get_diff_vs_base raises — we cannot determine the worktree state.
    mock_wt_mgr = MagicMock()
    mock_wt_mgr.get_diff_vs_base = AsyncMock(
        side_effect=RuntimeError("worktree gone / git error")
    )
    mock_worktree = tmp_path / "worktree"

    result = await ep._maybe_accept_approved_on_exhaustion(
        orch,
        task,
        exhausted_result,
        worktree=mock_worktree,
        worktree_mgr=mock_wt_mgr,
    )

    # The function must return a task (not None — None means "no-op, fall
    # through to retry/escalate", which is also wrong here), and that task
    # must be BLOCKED, not complete.
    assert result is not None, (
        "_maybe_accept_approved_on_exhaustion returned None (fell through to "
        "retry) when get_diff_vs_base raised — expected BLOCKED task"
    )
    assert result.status == "blocked", (
        f"expected status 'blocked' when diff-check raises, got {result.status!r} — "
        "task was silently completed without knowing whether approved changes exist"
    )
    assert result.blocked_reason is not None and "worktree_diff_check_failed" in result.blocked_reason, (
        f"expected blocked_reason to contain 'worktree_diff_check_failed', got {result.blocked_reason!r} — "
        "the A4 WORKTREE_DIFF_CHECK_FAILED class must be stamped in blocked_reason when diff-check raises"
    )
