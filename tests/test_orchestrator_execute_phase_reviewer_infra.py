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

import pytest

from adapters.types import AgentResult
from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from state.schemas import (
    AcceptanceCriterion,
    Phase,
    Plan,
    Task,
)

from stub_adapter import StubAdapter, ok


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
