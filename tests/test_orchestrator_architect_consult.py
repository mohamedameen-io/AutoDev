"""v0.26.1 patch G: ARCHITECT_CONSULT escalation rung — orchestrator tests.

The architect-consult rung sits between WEB_SEARCH and SOFT_BLOCKER. When
the autonomous escalation budget is exhausted (``search_count >= 3``), the
orchestrator re-delegates to ``architect_b`` in CONSULT MODE for a final
structured intervention. The architect returns ONE of three resolutions:

* ``RESOLUTION: refine-tasks`` — bullet list of corrective sub-tasks. The
  orchestrator injects them via the existing phase-review pipeline and
  marks the failing task as ``skipped`` (metadata reason
  ``architect_consult_refine_replacement``).
* ``RESOLUTION: infrastructure`` — environment / tooling diagnosis. The
  orchestrator marks the task ``escalated`` + ``blocked`` with the
  ``escalated_infra=True`` flag in metadata, mirroring SOFT_BLOCKER.
* ``RESOLUTION: continue`` — the developer was structurally on track.
  Reset the retry budget once and put the task back to ``in_progress``.

These tests cover the orchestration layer (helper invocation, ledger op
emission, status transitions) using stubbed adapter responses. The pure
ladder behavior lives in :mod:`tests.test_orchestrator_escalation_ladder_architect_consult`.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

import pytest

from adapters.types import AgentResult
from orchestrator import execute_phase as ep
from orchestrator.execute_phase import (
    ArchitectResolution,
    _parse_architect_resolution,
)
from state.plan_manager import PlanManager
from state.schemas import Phase, Plan, Task


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan(tasks: list[Task]) -> Plan:
    return Plan(
        plan_id="p-arch",
        spec_hash="cafe",
        phases=[Phase(id="1", title="arch", tasks=tasks)],
        created_at=_iso(),
        updated_at=_iso(),
    )


def _make_orch(tmp_path: Path, pm: PlanManager) -> Any:
    from config.defaults import default_config

    cfg = default_config()
    cfg.tournaments.execute_max_parallel_tasks = 1
    cfg.tournaments.phase_review.enabled = False

    class FakeAdapter:
        async def execute(self, inv):
            return AgentResult(
                success=True, text="ok\n", duration_s=0.01, files_changed=[], diff="",
            )

    class FakeRegistry:
        def get(self, role):
            from adapters.types import AgentSpec

            return AgentSpec(
                name=role,
                model="sonnet",
                prompt="prompt",
                description="",
                tools=[],
                max_turns=1,
            )

    class FakeKnowledge:
        async def inject_block(self, role, task_id=None):
            return ""

        async def record_tournament_event(self, event):
            return None

    class FakeGuard:
        def start_execute_phase(self, *a, **k):
            return None

        def execute_phase_wall_budget_exceeded(self, *a, **k):
            return False

        def check_execute_phase_wall_budget(self, *a, **k):
            return None

        def start_task(self, tid):
            pass

        def end_task(self, tid):
            pass

        def pre_invocation(self, *a, **kw):
            pass

        def post_invocation(self, *a, **kw):
            pass

    class FakeLoop:
        def observe(self, *a, **kw):
            pass

    orch = type(
        "Orch",
        (),
        {
            "cwd": tmp_path,
            "session_id": "test",
            "plan_manager": pm,
            "cfg": cfg,
            "guardrails": FakeGuard(),
            "adapter": FakeAdapter(),
            "registry": FakeRegistry(),
            "knowledge": FakeKnowledge(),
            "loop_detector": FakeLoop(),
            "plugin_registry": None,
            "disable_impl_tournament": True,
        },
    )()
    return orch


# ---------------------------------------------------------------------------
# Parser: _parse_architect_resolution
# ---------------------------------------------------------------------------


def test_parse_architect_resolution_refine_tasks() -> None:
    response = (
        "Looking at this, the task is over-scoped.\n"
        "Sub-tasks:\n"
        "- First sub-task\n"
        "  Body.\n"
        "- Second sub-task\n"
        "RESOLUTION: refine-tasks\n"
        "- First sub-task title\n"
        "  Touch files X.\n"
        "- Second sub-task title\n"
        "  Touch files Y.\n"
    )

    res = _parse_architect_resolution(response)

    assert res.action == "architect-refine"
    assert "- First sub-task title" in res.guidance


def test_parse_architect_resolution_infrastructure() -> None:
    response = (
        "I see three identical UnicodeDecodeError signals in the attempts.\n"
        "RESOLUTION: infrastructure\n"
        "Vendored Latin-1 file under External/SDL2 — skip the dir.\n"
    )
    res = _parse_architect_resolution(response)
    assert res.action == "architect-infra"
    # Guidance is the actionable content AFTER the RESOLUTION line per
    # the architect_b_consult.md prompt format.
    assert "Vendored Latin-1" in res.guidance


def test_parse_architect_resolution_continue() -> None:
    response = (
        "Attempt 2's diff was 90% right.\n"
        "RESOLUTION: continue\n"
        "Retry with the boundary check on line 80 fixed.\n"
    )
    res = _parse_architect_resolution(response)
    assert res.action == "architect-continue"


def test_parse_architect_resolution_malformed_falls_back_to_infra() -> None:
    """No RESOLUTION line at all → safe fallback to infra."""
    response = "I have no idea what to do here.\n"
    res = _parse_architect_resolution(response)
    assert res.action == "architect-infra"
    assert "no idea" in res.guidance


# ---------------------------------------------------------------------------
# Orchestration: _dispatch_architect_consult — refine path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_architect_refine_response_injects_corrective_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``RESOLUTION: refine-tasks`` with 2 bullets → ``append_corrective_tasks``
    called with 2 Task objects; failing task marked ``skipped``."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan(
            [
                Task(
                    id="1.1",
                    phase_id="1",
                    title="too big task",
                    description="too big",
                )
            ]
        )
    )
    orch = _make_orch(tmp_path, pm)
    # Side-effecting assertion: fetch the task and fail the test if absent.
    (await pm.get_task("1.1")) or pytest.fail("task not found")
    # Move to in_progress so the FSM transition to ``skipped`` is allowed.
    await pm.update_task_status("1.1", "in_progress")

    async def fake_escalate(*_a: object, **_kw: object) -> ArchitectResolution:
        return ArchitectResolution(
            action="architect-refine",
            guidance=(
                "- First subtask title\n"
                "  Touch a.py\n"
                "- Second subtask title\n"
                "  Touch b.py\n"
            ),
        )

    monkeypatch.setattr(ep, "_escalate_stuck_to_architect", fake_escalate)

    refreshed_task = (await pm.get_task("1.1")) or pytest.fail("task vanished")
    out = await ep._dispatch_architect_consult(
        orch,
        refreshed_task,
        stuck_state=await pm.get_stuck_state("1.1"),
        reason="failed",
        prior_attempts=None,
        web_context_block="",
    )

    assert out is not None
    assert out.status == "skipped"
    plan = await pm.load()
    assert plan is not None
    phase = plan.phases[0]
    # Two new corrective sub-tasks should have been appended.
    new_ids = [t.id for t in phase.tasks if t.id != "1.1"]
    assert len(new_ids) == 2, [t.id for t in phase.tasks]
    titles = [t.title for t in phase.tasks if t.id != "1.1"]
    assert "First subtask title" in titles
    assert "Second subtask title" in titles


# ---------------------------------------------------------------------------
# Orchestration: _dispatch_architect_consult — infrastructure path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_architect_infrastructure_response_marks_escalated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``RESOLUTION: infrastructure`` → task ``escalated`` + ``blocked``
    + ``escalated_infra=True`` flag in metadata."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan([Task(id="1.1", phase_id="1", title="t", description="d")])
    )
    orch = _make_orch(tmp_path, pm)
    await pm.update_task_status("1.1", "in_progress")
    task = (await pm.get_task("1.1")) or pytest.fail("task not found")

    async def fake_escalate(*_a: object, **_kw: object) -> ArchitectResolution:
        return ArchitectResolution(
            action="architect-infra",
            guidance="UTF-8 decode crash on vendored Latin-1 file",
        )

    monkeypatch.setattr(ep, "_escalate_stuck_to_architect", fake_escalate)

    out = await ep._dispatch_architect_consult(
        orch,
        task,
        stuck_state=await pm.get_stuck_state("1.1"),
        reason="utf8 decode failed",
        prior_attempts=None,
        web_context_block="",
    )

    assert out is not None
    assert out.status == "blocked"
    assert out.escalated is True
    assert out.blocked_reason and "architect_consult" in out.blocked_reason
    assert "UTF-8 decode" in (out.blocked_reason or "")


# ---------------------------------------------------------------------------
# Orchestration: _dispatch_architect_consult — continue path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_architect_continue_response_resets_retry_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``RESOLUTION: continue`` → retry_count zeroed; task in_progress."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan([Task(id="1.1", phase_id="1", title="t", description="d")])
    )
    orch = _make_orch(tmp_path, pm)
    await pm.update_task_status("1.1", "in_progress")
    # Bump retry_count to 5 so we can assert it's reset.
    for _ in range(5):
        await pm.mark_task_retry("1.1")
    task = (await pm.get_task("1.1")) or pytest.fail("task not found")
    assert task.retry_count == 5

    async def fake_escalate(*_a: object, **_kw: object) -> ArchitectResolution:
        return ArchitectResolution(
            action="architect-continue",
            guidance="The diff from attempt 2 was 80% right; retry.",
        )

    monkeypatch.setattr(ep, "_escalate_stuck_to_architect", fake_escalate)

    out = await ep._dispatch_architect_consult(
        orch,
        task,
        stuck_state=await pm.get_stuck_state("1.1"),
        reason="failed",
        prior_attempts=None,
        web_context_block="",
    )

    assert out is not None
    assert out.status == "in_progress"
    assert out.retry_count == 0


# ---------------------------------------------------------------------------
# Orchestration: ledger op + counter increment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_architect_consult_ledger_op_appended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``architect_consult`` ledger op fires regardless of resolution
    action (infra path used here as the simplest)."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan([Task(id="1.1", phase_id="1", title="t", description="d")])
    )
    orch = _make_orch(tmp_path, pm)
    await pm.update_task_status("1.1", "in_progress")
    task = (await pm.get_task("1.1")) or pytest.fail("task not found")

    captured: list[dict] = []
    real_append = pm.ledger_append

    async def spy(op: str, payload: dict) -> None:
        captured.append({"op": op, "payload": payload})
        await real_append(op, payload)

    monkeypatch.setattr(pm, "ledger_append", spy)

    async def fake_escalate(*_a: object, **_kw: object) -> ArchitectResolution:
        return ArchitectResolution(
            action="architect-infra", guidance="env failure"
        )

    monkeypatch.setattr(ep, "_escalate_stuck_to_architect", fake_escalate)

    await ep._dispatch_architect_consult(
        orch,
        task,
        stuck_state=await pm.get_stuck_state("1.1"),
        reason="failed",
        prior_attempts=None,
        web_context_block="",
    )

    consult_ops = [c for c in captured if c["op"] == "architect_consult"]
    assert consult_ops, f"no architect_consult ledger op found in {captured}"
    payload = consult_ops[0]["payload"]
    assert payload["task_id"] == "1.1"
    assert payload["action"] == "architect-infra"
    assert "env failure" in payload["architect_response_excerpt"]


@pytest.mark.asyncio
async def test_architect_consult_bumps_architect_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After ``_dispatch_architect_consult`` returns, the stuck state's
    ``architect_count`` is 1 so subsequent ladder evaluations route to
    SOFT_BLOCKER."""
    from orchestrator.escalation_ladder import next_step

    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan([Task(id="1.1", phase_id="1", title="t", description="d")])
    )
    orch = _make_orch(tmp_path, pm)
    await pm.update_task_status("1.1", "in_progress")
    task = (await pm.get_task("1.1")) or pytest.fail("task not found")

    async def fake_escalate(*_a: object, **_kw: object) -> ArchitectResolution:
        return ArchitectResolution(action="architect-infra", guidance="x")

    monkeypatch.setattr(ep, "_escalate_stuck_to_architect", fake_escalate)

    # Force a stuck state that triggers ARCHITECT_CONSULT on the first
    # call: search_count=3, architect_count=0.
    for _ in range(3):
        await pm.increment_search("1.1")
    state_before = await pm.get_stuck_state("1.1")
    assert state_before.architect_count == 0
    assert next_step(state_before) == "ARCHITECT_CONSULT"

    await ep._dispatch_architect_consult(
        orch,
        task,
        stuck_state=state_before,
        reason="failed",
        prior_attempts=None,
        web_context_block="",
    )

    state_after = await pm.get_stuck_state("1.1")
    assert state_after.architect_count == 1
    # And the ladder now routes to SOFT_BLOCKER.
    assert next_step(state_after) == "SOFT_BLOCKER"


# ---------------------------------------------------------------------------
# _escalate_stuck_to_architect: ARCHITECT_CONTEXT block contents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalate_stuck_to_architect_builds_correct_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ARCHITECT_CONTEXT block surfaces the task definition + counters
    in the prompt the architect_b adapter sees."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan(
            [
                Task(
                    id="1.1",
                    phase_id="1",
                    title="implement subtract",
                    description="needs to subtract",
                    files=["math.py"],
                )
            ]
        )
    )
    orch = _make_orch(tmp_path, pm)
    task = (await pm.get_task("1.1")) or pytest.fail("task not found")

    captured_prompts: list[str] = []

    class CaptureAdapter:
        async def execute(self, inv):
            captured_prompts.append(inv.prompt)
            return AgentResult(
                success=True,
                text="RESOLUTION: infrastructure\nx\n",
                duration_s=0.01,
                files_changed=[],
                diff="",
            )

    orch.adapter = CaptureAdapter()  # type: ignore[attr-defined]

    stuck_state = await pm.get_stuck_state("1.1")
    out = await ep._escalate_stuck_to_architect(
        orch,
        task,
        stuck_state=stuck_state,
        ladder_step="ARCHITECT_CONSULT",
        recent_evidence="some failure detail",
        prior_attempts=["retry_count=2, reason=coder failure"],
        typed_errors=["qa_gate_encoding_error: bad byte"],
    )

    assert out.action == "architect-infra"
    assert captured_prompts, "adapter never invoked"
    prompt = captured_prompts[0]
    assert "ARCHITECT_CONTEXT" in prompt
    assert "failing_task_id: 1.1" in prompt
    assert "ladder_step: ARCHITECT_CONSULT" in prompt
    assert "implement subtract" in prompt
    assert "qa_gate_encoding_error" in prompt


# ---------------------------------------------------------------------------
# _try_retry_or_escalate end-to-end: ARCHITECT_CONSULT branch dispatched
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_try_retry_dispatches_to_architect_consult_at_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the ladder returns ARCHITECT_CONSULT, the helper routes through
    ``_dispatch_architect_consult`` (NOT ``_escalate_stuck_to_critic``)."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan([Task(id="1.1", phase_id="1", title="t", description="d")])
    )
    orch = _make_orch(tmp_path, pm)
    task = (await pm.get_task("1.1")) or pytest.fail("task not found")

    # Set the ladder up for ARCHITECT_CONSULT on the next discard: pivot=2,
    # search=3 means the next call to next_step returns ARCHITECT_CONSULT.
    for _ in range(2):
        await pm.increment_pivot("1.1")
    for _ in range(3):
        await pm.increment_search("1.1")
    # discard_count will bump to 1 inside _try_retry_or_escalate — irrelevant
    # because architect rung beats it.

    dispatched: dict = {"called": False, "critic_called": False}

    async def fake_dispatch(*_a, **_kw):
        dispatched["called"] = True
        return task  # Return the task unchanged for the test.

    async def fake_critic(*_a, **_kw):
        dispatched["critic_called"] = True
        from orchestrator.execute_phase import StuckResolution

        return StuckResolution(action="refine", guidance="should never run")

    monkeypatch.setattr(ep, "_dispatch_architect_consult", fake_dispatch)
    monkeypatch.setattr(ep, "_escalate_stuck_to_critic", fake_critic)

    await ep._try_retry_or_escalate(
        orch, task, retry_limit=10, reason="failed",
        failure_class="worker_exception",
    )

    assert dispatched["called"] is True, "architect-consult dispatch did not fire"
    assert dispatched["critic_called"] is False, (
        "critic_sounding_board should NOT fire on the ARCHITECT_CONSULT rung"
    )
