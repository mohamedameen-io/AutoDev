"""Tier J (huge-repo): accept APPROVED-but-turn-exhausted research tasks.

The gap (358k-file repo): a Phase-0 research/confirmation task whose correct
output is an EMPTY diff already has a reviewer ``APPROVED`` verdict on record,
but the developer keeps hitting ``error_max_turns`` on broad codebase
exploration. The discard/escalation ladder eventually soft-blocks the task as
``user_decision_required`` — *losing* the already-approved result. The failure
is purely infrastructural turn-exhaustion, not a semantic verdict.

The fix (:func:`orchestrator.execute_phase._maybe_accept_approved_on_exhaustion`)
accepts the approved (empty-diff) artifact and completes the task, STRICTLY
gated so genuinely-failing tasks still block:

* (a) APPROVED on record + turn-exhausted        -> accepted / complete
* (b) NEEDS_CHANGES / REJECTED + turn-exhausted   -> still blocks
* (c) turn-exhausted with NO review verdict       -> still blocks
* (+) non-turn failure (e.g. auth)                -> still blocks
* (+) non-empty approved diff + turn-exhausted    -> falls through (no-op)
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from adapters.types import AgentResult
from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator import execute_phase as ep
from state.evidence import write_evidence
from state.schemas import (
    AcceptanceCriterion,
    CoderEvidence,
    Phase,
    Plan,
    ReviewEvidence,
    Task,
)

from stub_adapter import StubAdapter, fail, ok


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan() -> Plan:
    """One Phase-0 research task that legitimately produces no diff."""
    task = Task(
        id="0.1",
        phase_id="0",
        title="Confirm context-identity type + member inventory",
        description="Investigate; the correct output is an empty diff.",
        files=[],
        produces_diff=False,
        acceptance=[
            AcceptanceCriterion(id="ac-1", description="findings confirmed"),
        ],
    )
    return Plan(
        plan_id="p-accept",
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
    # Keep the "still blocks" regression tests fast: the default 30s retry
    # backoff would otherwise add ~90s per task driven to escalation.
    cfg.qa_retry_min_interval_s = 0.0
    registry = build_registry(cfg)
    orch = Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-accept",
    )
    await orch.plan_manager.init_plan(_mk_plan())
    return orch


def _max_turns_fail() -> AgentResult:
    """A developer result that exhausted its turn budget (no diff)."""
    return fail("agent hit max turns exploring the codebase",
                subtype="error_max_turns")


def _escalation_exhausted_fail() -> AgentResult:
    """The synthetic subtype the budget tracker returns once the ladder is spent."""
    return fail("budget escalation exhausted",
                subtype="error_max_turns_escalation_exhausted")


async def _seed_review(cwd: Path, task_id: str, verdict: str) -> None:
    await write_evidence(
        cwd,
        task_id,
        ReviewEvidence(
            task_id=task_id,
            verdict=verdict,  # type: ignore[arg-type]
            issues=[] if verdict == "APPROVED" else ["needs work"],
            output_text=f"VERDICT: {verdict}",
            raw_response=f"VERDICT: {verdict}",
        ),
    )


async def _seed_coder(cwd: Path, task_id: str, diff: str | None) -> None:
    await write_evidence(
        cwd,
        task_id,
        CoderEvidence(
            task_id=task_id,
            diff=diff,
            files_changed=[],
            output_text="empty-diff research artifact",
            success=True,
        ),
    )


# ---------------------------------------------------------------------------
# (a) APPROVED on record + escalation-exhausted -> accepted / complete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approved_plus_exhausted_is_accepted_not_blocked(
    tmp_path: Path,
) -> None:
    """The exact live gap: APPROVED empty diff + turn-exhaustion -> complete."""
    await _seed_review(tmp_path, "0.1", "APPROVED")
    await _seed_coder(tmp_path, "0.1", "")  # empty diff
    adapter = StubAdapter({"developer": _escalation_exhausted_fail()})
    orch = await _make_orch(tmp_path, adapter)

    tasks = await orch.execute()

    assert len(tasks) == 1
    task = tasks[0]
    assert task.status == "complete"
    assert task.escalated is False
    # The accept-on-exhaustion ledger breadcrumb was emitted.
    from state.ledger import read_entries

    ops = [e.op for e in read_entries(tmp_path)]
    assert "accepted_approved_on_exhaustion" in ops


@pytest.mark.asyncio
async def test_approved_plus_plain_max_turns_is_accepted(tmp_path: Path) -> None:
    """``error_max_turns`` (not yet escalation-exhausted) also accepts."""
    await _seed_review(tmp_path, "0.1", "APPROVED")
    await _seed_coder(tmp_path, "0.1", None)  # no recorded diff == empty
    adapter = StubAdapter({"developer": _max_turns_fail()})
    orch = await _make_orch(tmp_path, adapter)

    tasks = await orch.execute()

    assert tasks[0].status == "complete"
    assert tasks[0].escalated is False
    # Developer was dispatched exactly once — the accept short-circuits the
    # discard/retry loop immediately rather than re-burning turns.
    assert adapter.count("developer") == 1


# ---------------------------------------------------------------------------
# (b) NEEDS_CHANGES / REJECTED + exhausted -> still blocks (regression guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_needs_changes_plus_exhausted_still_blocks(tmp_path: Path) -> None:
    await _seed_review(tmp_path, "0.1", "NEEDS_CHANGES")
    await _seed_coder(tmp_path, "0.1", "")
    adapter = StubAdapter(
        {
            "developer": _escalation_exhausted_fail(),
            "critic_sounding_board": ok(
                "diagnosis\n\nRESOLUTION: soft-blocker\n"
            ),
        }
    )
    orch = await _make_orch(tmp_path, adapter)

    tasks = await orch.execute()

    assert tasks[0].status != "complete"
    assert tasks[0].escalated is True
    from state.ledger import read_entries

    ops = [e.op for e in read_entries(tmp_path)]
    assert "accepted_approved_on_exhaustion" not in ops


@pytest.mark.asyncio
async def test_rejected_plus_exhausted_still_blocks(tmp_path: Path) -> None:
    await _seed_review(tmp_path, "0.1", "REJECTED")
    await _seed_coder(tmp_path, "0.1", "")
    adapter = StubAdapter(
        {
            "developer": _escalation_exhausted_fail(),
            "critic_sounding_board": ok(
                "diagnosis\n\nRESOLUTION: soft-blocker\n"
            ),
        }
    )
    orch = await _make_orch(tmp_path, adapter)

    tasks = await orch.execute()

    assert tasks[0].status != "complete"
    assert tasks[0].escalated is True


# ---------------------------------------------------------------------------
# (c) exhausted with NO review verdict on record -> still blocks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exhausted_without_review_evidence_still_blocks(
    tmp_path: Path,
) -> None:
    # No review evidence seeded; the developer never succeeds so review
    # never runs and no APPROVED verdict can exist.
    adapter = StubAdapter(
        {
            "developer": _escalation_exhausted_fail(),
            "critic_sounding_board": ok(
                "diagnosis\n\nRESOLUTION: soft-blocker\n"
            ),
        }
    )
    orch = await _make_orch(tmp_path, adapter)

    tasks = await orch.execute()

    assert tasks[0].status != "complete"
    assert tasks[0].escalated is True


# ---------------------------------------------------------------------------
# (+) non-turn failure (auth) + APPROVED on record -> NOT accepted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_turn_failure_with_approved_is_not_accepted(
    tmp_path: Path,
) -> None:
    """A non-turn-exhaustion failure must NOT be auto-accepted even if an
    APPROVED verdict is on record — only infra turn-exhaustion qualifies.

    ``parse_error`` is a non-turn failure that does NOT trip the
    infrastructure circuit breaker, so the task drives the discard ladder
    to a soft-blocker rather than a quarantine halt.
    """
    await _seed_review(tmp_path, "0.1", "APPROVED")
    await _seed_coder(tmp_path, "0.1", "")
    adapter = StubAdapter(
        {
            "developer": fail("could not parse response",
                              subtype="parse_error"),
            "critic_sounding_board": ok(
                "diagnosis\n\nRESOLUTION: soft-blocker\n"
            ),
        }
    )
    orch = await _make_orch(tmp_path, adapter)

    tasks = await orch.execute()

    assert tasks[0].status != "complete"
    from state.ledger import read_entries

    ops = [e.op for e in read_entries(tmp_path)]
    assert "accepted_approved_on_exhaustion" not in ops


# ---------------------------------------------------------------------------
# (+) non-empty APPROVED diff + exhausted -> falls through (out of scope)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nonempty_in_hand_diff_falls_through(tmp_path: Path) -> None:
    """A turn-exhausted attempt that nonetheless carries a non-empty in-hand
    diff is deliberately NOT auto-accepted (applying an un-reviewed partial
    diff would be unsafe); it falls through to the unchanged retry/escalate
    path. The gate keys off ``developer_result.diff`` of the attempt being
    handled, not stale recorded evidence."""
    await _seed_review(tmp_path, "0.1", "APPROVED")
    nonempty = AgentResult(
        success=False,
        text="",
        duration_s=0.01,
        error="hit max turns but emitted a partial patch",
        subtype="error_max_turns",
        diff="diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@\n+x=1\n",
    )
    adapter = StubAdapter(
        {
            "developer": nonempty,
            "critic_sounding_board": ok(
                "diagnosis\n\nRESOLUTION: soft-blocker\n"
            ),
        }
    )
    orch = await _make_orch(tmp_path, adapter)

    tasks = await orch.execute()

    assert tasks[0].status != "complete"
    from state.ledger import read_entries

    ops = [e.op for e in read_entries(tmp_path)]
    assert "accepted_approved_on_exhaustion" not in ops


# ---------------------------------------------------------------------------
# Helper-level unit coverage (right blast radius)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_helper_returns_none_on_non_turn_subtype(tmp_path: Path) -> None:
    await _seed_review(tmp_path, "0.1", "APPROVED")
    await _seed_coder(tmp_path, "0.1", "")
    adapter = StubAdapter({})
    orch = await _make_orch(tmp_path, adapter)
    task = await orch.plan_manager.get_task("0.1")
    assert task is not None

    result = await ep._maybe_accept_approved_on_exhaustion(
        orch, task, fail("boom", subtype="parse_error")
    )
    assert result is None


@pytest.mark.asyncio
async def test_helper_accepts_and_resets_stuck_state(tmp_path: Path) -> None:
    await _seed_review(tmp_path, "0.1", "APPROVED")
    await _seed_coder(tmp_path, "0.1", "")
    adapter = StubAdapter({})
    orch = await _make_orch(tmp_path, adapter)
    # Drive the task to in_progress (the state at the developer-failure site)
    # and accrue a discard so we can prove the accept path zeroes it.
    await orch.plan_manager.update_task_status("0.1", "in_progress")
    await orch.plan_manager.increment_discard("0.1")
    task = await orch.plan_manager.get_task("0.1")
    assert task is not None

    result = await ep._maybe_accept_approved_on_exhaustion(
        orch, task, _escalation_exhausted_fail()
    )
    assert result is not None
    assert result.status == "complete"
    stuck = await orch.plan_manager.get_stuck_state("0.1")
    assert stuck.discard_count == 0
