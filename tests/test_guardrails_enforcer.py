"""Tests for :class:`src.guardrails.enforcer.GuardrailEnforcer`."""

from __future__ import annotations

from pathlib import Path

import pytest

from adapters.types import AgentInvocation, AgentResult
from config.schema import GuardrailsConfig
from errors import GuardrailExceededError
from guardrails.enforcer import GuardrailEnforcer


def _cfg(**kwargs) -> GuardrailsConfig:
    defaults = {
        "max_tool_calls_per_task": 10,
        "max_duration_s_per_task": 60,
        "max_diff_bytes": 1024,
    }
    defaults.update(kwargs)
    return GuardrailsConfig(**defaults)


def _inv() -> AgentInvocation:
    return AgentInvocation(role="developer", prompt="do it", cwd=Path("/tmp"))


def _result(tool_calls: int = 0, diff: str | None = None) -> AgentResult:
    from adapters.types import ToolCall
    calls = [ToolCall(tool=f"tool_{i}") for i in range(tool_calls)]
    return AgentResult(
        success=True,
        text="done",
        tool_calls=calls,
        diff=diff,
        duration_s=0.01,
    )


def test_start_and_end_task() -> None:
    enf = GuardrailEnforcer(_cfg())
    assert not enf.is_tracking("t1")
    enf.start_task("t1")
    assert enf.is_tracking("t1")
    enf.end_task("t1")
    assert not enf.is_tracking("t1")


def test_end_task_idempotent() -> None:
    enf = GuardrailEnforcer(_cfg())
    enf.end_task("never-started")  # should not raise


def test_pre_invocation_untracked_is_lenient() -> None:
    enf = GuardrailEnforcer(_cfg())
    # No start_task — should not raise.
    enf.pre_invocation("untracked", _inv())


def test_post_invocation_untracked_is_lenient() -> None:
    enf = GuardrailEnforcer(_cfg())
    enf.post_invocation("untracked", _result())


def test_tool_call_cap_exceeded() -> None:
    enf = GuardrailEnforcer(_cfg(max_tool_calls_per_task=3))
    enf.start_task("t1")
    enf.pre_invocation("t1", _inv())
    enf.post_invocation("t1", _result(tool_calls=2))
    enf.pre_invocation("t1", _inv())
    with pytest.raises(GuardrailExceededError, match="tool-call cap"):
        enf.post_invocation("t1", _result(tool_calls=2))


def test_invocation_cap_exceeded() -> None:
    enf = GuardrailEnforcer(_cfg(max_tool_calls_per_task=2))
    enf.start_task("t1")
    # Exhaust invocation count without tool calls.
    for _ in range(2):
        enf.pre_invocation("t1", _inv())
        enf.post_invocation("t1", _result(tool_calls=0))
    with pytest.raises(GuardrailExceededError, match="invocation cap"):
        enf.pre_invocation("t1", _inv())


def test_diff_size_cap_exceeded() -> None:
    enf = GuardrailEnforcer(_cfg(max_diff_bytes=10))
    enf.start_task("t1")
    enf.pre_invocation("t1", _inv())
    with pytest.raises(GuardrailExceededError, match="diff-size cap"):
        enf.post_invocation("t1", _result(diff="x" * 20))


def test_diff_size_cumulative() -> None:
    enf = GuardrailEnforcer(_cfg(max_diff_bytes=15))
    enf.start_task("t1")
    enf.pre_invocation("t1", _inv())
    enf.post_invocation("t1", _result(diff="x" * 8))
    enf.pre_invocation("t1", _inv())
    with pytest.raises(GuardrailExceededError, match="diff-size cap"):
        enf.post_invocation("t1", _result(diff="x" * 10))


def test_metrics_snapshot_empty_when_not_tracking() -> None:
    enf = GuardrailEnforcer(_cfg())
    assert enf.metrics_snapshot("unknown") == {}


def test_metrics_snapshot_populated() -> None:
    enf = GuardrailEnforcer(_cfg())
    enf.start_task("t1")
    enf.pre_invocation("t1", _inv())
    enf.post_invocation("t1", _result(tool_calls=3, diff="abc"))
    snap = enf.metrics_snapshot("t1")
    assert snap["tool_call_count"] == 3
    assert snap["invocation_count"] == 1
    assert snap["total_diff_bytes"] == 3
    assert snap["elapsed_s"] >= 0


def test_start_task_resets_metrics() -> None:
    enf = GuardrailEnforcer(_cfg())
    enf.start_task("t1")
    enf.pre_invocation("t1", _inv())
    enf.post_invocation("t1", _result(tool_calls=5))
    # Re-start should reset.
    enf.start_task("t1")
    snap = enf.metrics_snapshot("t1")
    assert snap["tool_call_count"] == 0
    assert snap["invocation_count"] == 0
