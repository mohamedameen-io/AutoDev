"""Tests for adapter pydantic types."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from adapters.types import (
    AgentInvocation,
    AgentResult,
    AgentSpec,
    StreamEvent,
    ToolCall,
)


def test_agent_invocation_roundtrip() -> None:
    inv = AgentInvocation(
        role="developer",
        prompt="add a subtract function",
        cwd=Path("/tmp/foo"),
        model="sonnet",
        timeout_s=120,
        allowed_tools=["Read", "Edit", "Bash"],
        max_turns=3,
        metadata={"task_id": "t1.2", "attempt": 1},
    )
    dumped = inv.model_dump(mode="json")
    reloaded = AgentInvocation.model_validate(dumped)
    assert reloaded.role == "developer"
    assert reloaded.model == "sonnet"
    assert reloaded.allowed_tools == ["Read", "Edit", "Bash"]
    assert reloaded.metadata["task_id"] == "t1.2"
    # cwd round-trips as Path (pydantic coerces string back into Path).
    assert isinstance(reloaded.cwd, Path)
    assert reloaded.cwd == Path("/tmp/foo")


def test_agent_invocation_accepts_path_object() -> None:
    inv = AgentInvocation(role="r", prompt="p", cwd=Path("/x"))
    assert isinstance(inv.cwd, Path)


def test_agent_invocation_coerces_string_cwd() -> None:
    inv = AgentInvocation.model_validate(
        {"role": "r", "prompt": "p", "cwd": "/x/y"}
    )
    assert isinstance(inv.cwd, Path)
    assert inv.cwd == Path("/x/y")


def test_agent_invocation_defaults() -> None:
    inv = AgentInvocation(role="r", prompt="p", cwd=Path("/x"))
    assert inv.model is None
    assert inv.timeout_s == 600
    assert inv.allowed_tools is None
    assert inv.max_turns == 1
    assert inv.metadata == {}


def test_agent_result_json_roundtrip() -> None:
    res = AgentResult(
        success=True,
        text="hello",
        tool_calls=[
            ToolCall(tool="Read", args={"path": "/tmp/x.txt"}, result_summary="ok"),
            ToolCall(
                tool="Edit",
                args={"file": "/a.py", "hunks": [{"old": "a", "new": "b"}]},
                error=None,
            ),
        ],
        files_changed=[Path("a.py"), Path("b.py")],
        diff="--- a/x\n+++ b/x\n",
        duration_s=1.234,
        error=None,
        raw_stdout="stdout",
        raw_stderr="stderr",
    )
    blob = res.model_dump_json()
    reloaded = AgentResult.model_validate(json.loads(blob))
    assert reloaded.success is True
    assert reloaded.text == "hello"
    assert len(reloaded.tool_calls) == 2
    assert reloaded.tool_calls[1].args["hunks"][0]["new"] == "b"
    assert reloaded.files_changed == [Path("a.py"), Path("b.py")]
    assert reloaded.duration_s == pytest.approx(1.234)


def test_tool_call_nested_dicts() -> None:
    tc = ToolCall(
        tool="Bash",
        args={
            "command": "git status",
            "options": {"timeout_ms": 5000, "env": {"CI": "1"}},
        },
    )
    dumped = tc.model_dump_json()
    reloaded = ToolCall.model_validate_json(dumped)
    assert reloaded.args["options"]["env"]["CI"] == "1"


def test_agent_result_defaults() -> None:
    res = AgentResult(success=False, text="", duration_s=0.0)
    assert res.tool_calls == []
    assert res.files_changed == []
    assert res.diff is None
    assert res.error is None
    assert res.raw_stdout == ""
    assert res.raw_stderr == ""


def test_agent_spec_defaults() -> None:
    spec = AgentSpec(name="developer", description="d", prompt="p")
    assert spec.tools == []
    assert spec.model is None


def test_stream_event_type_literal() -> None:
    ev = StreamEvent(type="text", data={"text": "hello"})
    assert ev.type == "text"
    with pytest.raises(ValidationError):
        StreamEvent(type="bogus", data={})  # type: ignore[arg-type]


def test_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        AgentInvocation(
            role="r",
            prompt="p",
            cwd=Path("/x"),
            unexpected_field="oops",  # type: ignore[call-arg]
        )
