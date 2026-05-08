"""Tests for v0.11.0 conflict escalation helpers in execute_phase.

Covers:

* :func:`_parse_conflict_resolution` — directive extraction + parser
  fallback to ``abandon-task`` on malformed input.
* :func:`_escalate_conflict_to_critic` — builds the right envelope and
  forwards the parsed resolution.

The ``_execute_one_worker`` integration with apply_patch_to_main is
exercised in commit 14's tests (test_orchestrator_conflict_escalation_wired.py).
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

import pytest

from orchestrator import execute_phase as ep
from orchestrator.execute_phase import (
    ConflictResolution,
    _parse_conflict_resolution,
)
from state.plan_manager import PlanManager
from state.schemas import Phase, Plan, Task


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# _parse_conflict_resolution
# ---------------------------------------------------------------------------


def test_parse_resolution_rebase_and_retry() -> None:
    """Plain rebase-and-retry directive."""
    text = "Some preamble.\n\nRESOLUTION: rebase-and-retry\n"
    out = _parse_conflict_resolution(text)
    assert out.action == "rebase-and-retry"
    assert out.rewrite_guidance == ""


def test_parse_resolution_abandon_task() -> None:
    """Abandon-task directive."""
    text = "Cannot reconcile.\n\nRESOLUTION: abandon-task\n"
    out = _parse_conflict_resolution(text)
    assert out.action == "abandon-task"
    assert out.rewrite_guidance == ""


def test_parse_resolution_rewrite_captures_guidance() -> None:
    """Rewrite directive: lines before the directive are captured as guidance."""
    text = (
        "Re-implement using the lazy-import pattern.\n"
        "Move the yaml import inside the function body.\n"
        "\n"
        "RESOLUTION: rewrite\n"
    )
    out = _parse_conflict_resolution(text)
    assert out.action == "rewrite"
    assert "lazy-import pattern" in out.rewrite_guidance
    assert "yaml" in out.rewrite_guidance


def test_parse_resolution_unparseable_falls_back_to_abandon() -> None:
    """No directive found → defensive default of abandon-task."""
    out = _parse_conflict_resolution("Just some random text without a directive.")
    assert out.action == "abandon-task"


def test_parse_resolution_empty_string_falls_back_to_abandon() -> None:
    """Empty critic response → abandon-task."""
    out = _parse_conflict_resolution("")
    assert out.action == "abandon-task"


def test_parse_resolution_picks_last_directive_when_multiple() -> None:
    """If the response has multiple RESOLUTION: lines, the LAST one wins."""
    text = (
        "Initial thought:\nRESOLUTION: rebase-and-retry\n\n"
        "Actually no:\nRESOLUTION: abandon-task\n"
    )
    out = _parse_conflict_resolution(text)
    assert out.action == "abandon-task"


def test_parse_resolution_directive_must_be_on_own_line() -> None:
    """``RESOLUTION: rewrite`` mid-line is NOT matched (must be anchored)."""
    text = "Some text RESOLUTION: rewrite within a sentence does not count.\n"
    out = _parse_conflict_resolution(text)
    assert out.action == "abandon-task"


# ---------------------------------------------------------------------------
# _escalate_conflict_to_critic
# ---------------------------------------------------------------------------


def _mk_plan(tasks: list[Task]) -> Plan:
    return Plan(
        plan_id="p-conflict",
        spec_hash="cafe",
        phases=[Phase(id="1", title="conflict", tasks=tasks)],
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

            # Default response: rebase-and-retry. Tests can override.
            response = captured.get("next_response", "RESOLUTION: rebase-and-retry\n")
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
async def test_escalate_builds_conflict_context_block(tmp_path: Path) -> None:
    """The envelope passed to delegate() contains a CONFLICT_CONTEXT block."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan(
            [Task(id="1.1", phase_id="1", title="t", description="d")]
        )
    )
    orch = _make_orch(tmp_path, pm)
    task = (await pm.get_task("1.1")) or pytest.fail("task not found")

    out = await ep._escalate_conflict_to_critic(
        orch,
        task,
        worktree=tmp_path / "wt",
        conflict_diff="diff --git a/foo b/foo\n+new line\n",
        already_applied_diff="diff --git a/foo b/foo\n+other line\n",
        conflict_files=["foo.py", "bar.py"],
    )
    # Default fake adapter returns rebase-and-retry.
    assert out.action == "rebase-and-retry"
    # Verify the prompt includes the CONFLICT_CONTEXT marker + file list.
    prompt = orch._captured["prompts"][0]
    assert "CONFLICT_CONTEXT:" in prompt
    assert "failing_task_id: 1.1" in prompt
    assert "foo.py" in prompt
    assert "bar.py" in prompt


@pytest.mark.asyncio
async def test_escalate_returns_abandon_when_critic_says_so(tmp_path: Path) -> None:
    """Critic returning RESOLUTION: abandon-task surfaces as that action."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan([Task(id="1.1", phase_id="1", title="t", description="d")])
    )
    orch = _make_orch(tmp_path, pm)
    orch._captured["next_response"] = (
        "Cannot reconcile.\n\nRESOLUTION: abandon-task\n"
    )
    task = (await pm.get_task("1.1")) or pytest.fail()
    out = await ep._escalate_conflict_to_critic(
        orch,
        task,
        worktree=tmp_path / "wt",
        conflict_diff="",
    )
    assert out.action == "abandon-task"


@pytest.mark.asyncio
async def test_escalate_returns_rewrite_with_guidance(tmp_path: Path) -> None:
    """Critic returning rewrite includes the preceding text as guidance."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan([Task(id="1.1", phase_id="1", title="t", description="d")])
    )
    orch = _make_orch(tmp_path, pm)
    orch._captured["next_response"] = (
        "Use lazy yaml import inside parse_config.\n\n"
        "RESOLUTION: rewrite\n"
    )
    task = (await pm.get_task("1.1")) or pytest.fail()
    out = await ep._escalate_conflict_to_critic(
        orch,
        task,
        worktree=tmp_path / "wt",
        conflict_diff="",
    )
    assert out.action == "rewrite"
    assert "lazy yaml" in out.rewrite_guidance


def test_conflict_resolution_default_action_is_abandon() -> None:
    """The dataclass default action is abandon-task (defensive)."""
    out = ConflictResolution()
    assert out.action == "abandon-task"
    assert out.rewrite_guidance == ""
