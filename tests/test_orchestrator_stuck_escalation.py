"""v0.15.0: ``_escalate_stuck_to_critic`` helper + ``_parse_stuck_resolution`` parser.

Mirrors :mod:`tests.test_orchestrator_conflict_escalation` for the new
STUCK RECOVERY MODE escalation path. The helper:
* Builds a ``DelegationEnvelope`` carrying a ``STUCK_CONTEXT:`` block.
* Invokes ``critic_sounding_board`` via :func:`delegate`.
* Parses the response via :func:`_parse_stuck_resolution`.
* Returns a :class:`StuckResolution` with one of three actions:
  ``"refine"``, ``"pivot"``, ``"soft-blocker"``.

Defensive defaults: an unparseable critic response → ``"refine"`` (the
least-disruptive fallback — same documented contract as the prompt).
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

import pytest

from orchestrator import execute_phase as ep
from orchestrator.escalation_ladder import StuckState
from orchestrator.execute_phase import (
    StuckResolution,
    _parse_stuck_resolution,
)
from state.plan_manager import PlanManager
from state.schemas import Phase, Plan, Task


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# _parse_stuck_resolution
# ---------------------------------------------------------------------------


def test_parse_resolution_refine_captures_guidance() -> None:
    text = (
        "Add a return-annotation hint and require the developer to "
        "include type hints on the new parameter.\n\n"
        "RESOLUTION: refine\n"
    )
    out = _parse_stuck_resolution(text)
    assert out.action == "refine"
    assert "type hints" in out.guidance


def test_parse_resolution_pivot_captures_guidance() -> None:
    text = (
        "Stop using shell=True. Build the argv list explicitly and pass\n"
        "it to subprocess.run as a list.\n\n"
        "RESOLUTION: pivot\n"
    )
    out = _parse_stuck_resolution(text)
    assert out.action == "pivot"
    assert "subprocess.run" in out.guidance


def test_parse_resolution_soft_blocker_captures_guidance() -> None:
    text = (
        "What the human needs to decide: which target hardware family "
        "should be the reference for production behavior?\n\n"
        "RESOLUTION: soft-blocker\n"
    )
    out = _parse_stuck_resolution(text)
    assert out.action == "soft-blocker"
    assert "target hardware" in out.guidance


def test_parse_resolution_empty_falls_back_to_refine() -> None:
    """Empty response → defensive default of ``refine`` (least-disruptive)."""
    out = _parse_stuck_resolution("")
    assert out.action == "refine"


def test_parse_resolution_unparseable_falls_back_to_refine() -> None:
    out = _parse_stuck_resolution("Just some random text without a directive.")
    assert out.action == "refine"


def test_parse_resolution_picks_last_directive_when_multiple() -> None:
    text = (
        "Initial thought:\nRESOLUTION: refine\n\n"
        "Actually no:\nRESOLUTION: pivot\n"
    )
    out = _parse_stuck_resolution(text)
    assert out.action == "pivot"


def test_parse_resolution_directive_must_be_on_own_line() -> None:
    text = "Some text RESOLUTION: pivot within a sentence does not count.\n"
    out = _parse_stuck_resolution(text)
    assert out.action == "refine"


# ---------------------------------------------------------------------------
# _escalate_stuck_to_critic
# ---------------------------------------------------------------------------


def _mk_plan(tasks: list[Task]) -> Plan:
    return Plan(
        plan_id="p-stuck",
        spec_hash="cafe",
        phases=[Phase(id="1", title="stuck", tasks=tasks)],
        created_at=_iso(),
        updated_at=_iso(),
    )


def _make_orch(tmp_path: Path, pm: PlanManager) -> Any:
    from config.defaults import default_config

    cfg = default_config()
    cfg.tournaments.execute_max_parallel_tasks = 1
    cfg.tournaments.phase_review.enabled = False

    captured: dict = {"prompts": []}

    class FakeAdapter:
        async def execute(self, inv):
            captured["prompts"].append(inv.prompt)
            captured["last_inv"] = inv
            from adapters.types import AgentResult

            response = captured.get("next_response", "RESOLUTION: refine\n")
            return AgentResult(
                success=True,
                text=response,
                duration_s=0.01,
                files_changed=[],
                diff="",
            )

    class FakeRegistry:
        def get(self, role):
            from adapters.types import AgentSpec

            return AgentSpec(
                name=role,
                model="sonnet",
                prompt="critic prompt",
                description="",
                tools=[],
                max_turns=1,
            )

    class FakeKnowledge:
        async def inject_block(self, role, task_id=None):
            return ""

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
            "_captured": captured,
        },
    )()
    return orch


@pytest.mark.asyncio
async def test_escalate_stuck_invokes_critic_with_stuck_context(
    tmp_path: Path,
) -> None:
    """The envelope passed to delegate() contains a STUCK_CONTEXT block."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan(
            [Task(id="1.1", phase_id="1", title="t", description="d")]
        )
    )
    orch = _make_orch(tmp_path, pm)
    task = (await pm.get_task("1.1")) or pytest.fail("task not found")

    state = StuckState(discard_count=3, pivot_count=0, last_event="discard")
    out = await ep._escalate_stuck_to_critic(
        orch,
        task,
        stuck_state=state,
        ladder_step="REFINE",
        recent_evidence="reviewer flagged missing type hints",
        prior_attempts=["attempt 1: missing hint", "attempt 2: still missing"],
    )
    assert out.action == "refine"  # default fake-adapter response
    prompt = orch._captured["prompts"][0]
    assert "STUCK_CONTEXT:" in prompt
    assert "failing_task_id: 1.1" in prompt
    assert "discard_count: 3" in prompt
    assert "pivot_count: 0" in prompt
    assert "ladder_step: REFINE" in prompt
    assert "missing type hints" in prompt


@pytest.mark.asyncio
async def test_critic_resolution_pivot_dispatches_radical_redirect(
    tmp_path: Path,
) -> None:
    """Critic returning ``RESOLUTION: pivot`` surfaces as the pivot action."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan([Task(id="1.1", phase_id="1", title="t", description="d")])
    )
    orch = _make_orch(tmp_path, pm)
    task = (await pm.get_task("1.1")) or pytest.fail("task not found")
    orch._captured["next_response"] = (
        "Stop using shell=True. Use argv list directly.\n\n"
        "RESOLUTION: pivot\n"
    )

    state = StuckState(discard_count=5, pivot_count=0)
    out = await ep._escalate_stuck_to_critic(
        orch,
        task,
        stuck_state=state,
        ladder_step="PIVOT",
        recent_evidence="3 consecutive shell=True failures",
    )
    assert out.action == "pivot"
    assert "shell=True" in out.guidance


@pytest.mark.asyncio
async def test_critic_resolution_soft_blocker_marks_task_blocked(
    tmp_path: Path,
) -> None:
    """Critic returning ``RESOLUTION: soft-blocker`` surfaces as soft-blocker."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan([Task(id="1.1", phase_id="1", title="t", description="d")])
    )
    orch = _make_orch(tmp_path, pm)
    task = (await pm.get_task("1.1")) or pytest.fail("task not found")
    orch._captured["next_response"] = (
        "What the human needs to decide: which hardware family.\n\n"
        "RESOLUTION: soft-blocker\n"
    )

    state = StuckState(discard_count=5, pivot_count=3)
    out = await ep._escalate_stuck_to_critic(
        orch,
        task,
        stuck_state=state,
        ladder_step="SOFT_BLOCKER",
        recent_evidence="3 pivots in a row failed",
    )
    assert out.action == "soft-blocker"
    assert "hardware" in out.guidance


def test_stuck_resolution_default_action_is_refine() -> None:
    """The defensive default of :class:`StuckResolution` is ``refine`` —
    matches the documented prompt fallback."""
    out = StuckResolution()
    assert out.action == "refine"
    assert out.guidance == ""
