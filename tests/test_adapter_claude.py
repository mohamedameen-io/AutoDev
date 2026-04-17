"""Tests for the Claude Code subprocess adapter (subprocess mocked)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from adapters.claude_code import ClaudeCodeAdapter
from adapters.types import AgentInvocation


def _fake_proc(
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
    hang: bool = False,
) -> AsyncMock:
    """Create an AsyncMock that mimics asyncio.subprocess.Process."""
    proc = AsyncMock()
    proc.returncode = returncode
    if hang:
        async def _never(*_a, **_kw):  # pragma: no cover - timing path
            await asyncio.sleep(3600)
        proc.communicate = _never
    else:
        proc.communicate = AsyncMock(
            return_value=(stdout.encode("utf-8"), stderr.encode("utf-8"))
        )
    proc.wait = AsyncMock(return_value=returncode)
    proc.kill = lambda: None
    return proc


def _good_claude_blob(text: str = "PING_OK") -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": 123,
            "num_turns": 1,
            "result": text,
            "stop_reason": "end_turn",
            "session_id": "00000000-0000-0000-0000-000000000000",
            "total_cost_usd": 0.0,
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "modelUsage": {},
            "permission_denials": [],
            "terminal_reason": "completed",
            "uuid": "11111111-1111-1111-1111-111111111111",
        }
    )


@pytest.mark.asyncio
async def test_execute_successful_call(tmp_path: Path) -> None:
    adapter = ClaudeCodeAdapter()
    inv = AgentInvocation(
        role="echo",
        prompt="Reply with exactly: PING_OK",
        cwd=tmp_path,
        model="haiku",
        max_turns=1,
    )
    fake = _fake_proc(stdout=_good_claude_blob("PING_OK"), returncode=0)
    with patch(
        "adapters.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake),
    ):
        result = await adapter.execute(inv)
    assert result.success is True
    assert result.text == "PING_OK"
    assert result.error is None
    assert result.raw_stdout  # preserved


@pytest.mark.asyncio
async def test_execute_malformed_json(tmp_path: Path) -> None:
    adapter = ClaudeCodeAdapter()
    inv = AgentInvocation(role="r", prompt="p", cwd=tmp_path, max_turns=1)
    fake = _fake_proc(stdout="not json at all", returncode=0)
    with patch(
        "adapters.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake),
    ):
        result = await adapter.execute(inv)
    assert result.success is False
    assert result.error is not None
    assert "parse failed" in result.error
    assert result.raw_stdout == "not json at all"


@pytest.mark.asyncio
async def test_execute_timeout(tmp_path: Path) -> None:
    adapter = ClaudeCodeAdapter()
    inv = AgentInvocation(role="r", prompt="p", cwd=tmp_path, timeout_s=1)
    fake = _fake_proc(hang=True)
    with patch(
        "adapters.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake),
    ):
        result = await adapter.execute(inv)
    assert result.success is False
    assert result.error is not None
    assert "timeout" in result.error.lower()


@pytest.mark.asyncio
async def test_execute_nonzero_exit(tmp_path: Path) -> None:
    adapter = ClaudeCodeAdapter()
    inv = AgentInvocation(role="r", prompt="p", cwd=tmp_path)
    fake = _fake_proc(stdout="", stderr="auth failed", returncode=2)
    with patch(
        "adapters.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake),
    ):
        result = await adapter.execute(inv)
    assert result.success is False
    assert result.error is not None
    assert "auth failed" in result.error
    assert result.raw_stderr == "auth failed"


@pytest.mark.asyncio
async def test_execute_is_error_true(tmp_path: Path) -> None:
    adapter = ClaudeCodeAdapter()
    inv = AgentInvocation(role="r", prompt="p", cwd=tmp_path)
    blob = json.dumps({"result": "", "is_error": True, "error": "rate_limited"})
    fake = _fake_proc(stdout=blob, returncode=0)
    with patch(
        "adapters.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake),
    ):
        result = await adapter.execute(inv)
    assert result.success is False
    assert result.error == "rate_limited"


@pytest.mark.asyncio
async def test_execute_binary_not_found(tmp_path: Path) -> None:
    adapter = ClaudeCodeAdapter(binary="nonexistent-claude-binary-xyz")
    inv = AgentInvocation(role="r", prompt="p", cwd=tmp_path)
    with patch(
        "adapters.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(side_effect=FileNotFoundError("no such file")),
    ):
        result = await adapter.execute(inv)
    assert result.success is False
    assert result.error is not None
    assert "not found" in result.error


def test_build_command_basic(tmp_path: Path) -> None:
    adapter = ClaudeCodeAdapter()
    inv = AgentInvocation(role="r", prompt="say hi", cwd=tmp_path, max_turns=1)
    cmd = adapter._build_command(inv)
    assert cmd[0] == "claude"
    assert "-p" in cmd
    assert "say hi" in cmd
    assert "--output-format" in cmd and "json" in cmd
    assert "--permission-mode" in cmd and "acceptEdits" in cmd
    assert "--max-turns" in cmd
    assert "--continue" not in cmd


def test_build_command_with_model_and_tools(tmp_path: Path) -> None:
    adapter = ClaudeCodeAdapter()
    inv = AgentInvocation(
        role="developer",
        prompt="do stuff",
        cwd=tmp_path,
        model="sonnet",
        max_turns=3,
        allowed_tools=["Read", "Edit", "Bash"],
    )
    cmd = adapter._build_command(inv)
    assert "--model" in cmd
    idx = cmd.index("--model")
    assert cmd[idx + 1] == "sonnet"
    assert "--allowed-tools" in cmd
    idx2 = cmd.index("--allowed-tools")
    assert cmd[idx2 + 1] == "Read,Edit,Bash"
    idx3 = cmd.index("--max-turns")
    assert cmd[idx3 + 1] == "3"


def test_build_command_omits_optional_flags(tmp_path: Path) -> None:
    adapter = ClaudeCodeAdapter()
    inv = AgentInvocation(role="r", prompt="p", cwd=tmp_path)
    inv_no_turns = inv.model_copy(update={"max_turns": 0})
    cmd = adapter._build_command(inv_no_turns)
    assert "--max-turns" not in cmd
    assert "--model" not in cmd
    assert "--allowed-tools" not in cmd


@pytest.mark.asyncio
async def test_healthcheck_success() -> None:
    adapter = ClaudeCodeAdapter()
    fake = _fake_proc(stdout="2.1.92 (Claude Code)\n", returncode=0)
    with patch(
        "adapters.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake),
    ):
        ok, details = await adapter.healthcheck()
    assert ok is True
    assert "2.1.92" in details


@pytest.mark.asyncio
async def test_healthcheck_missing_binary() -> None:
    adapter = ClaudeCodeAdapter(binary="no-such-bin")
    with patch(
        "adapters.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(side_effect=FileNotFoundError()),
    ):
        ok, details = await adapter.healthcheck()
    assert ok is False
    assert "not found" in details


@pytest.mark.asyncio
async def test_healthcheck_nonzero() -> None:
    adapter = ClaudeCodeAdapter()
    fake = _fake_proc(stdout="", stderr="something bad", returncode=1)
    with patch(
        "adapters.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake),
    ):
        ok, details = await adapter.healthcheck()
    assert ok is False
    assert "something bad" in details


@pytest.mark.asyncio
async def test_init_workspace_is_stub(tmp_path: Path) -> None:
    adapter = ClaudeCodeAdapter()
    # Should not raise, even with arbitrary inputs.
    await adapter.init_workspace(tmp_path, [])


@pytest.mark.asyncio
async def test_execute_non_git_repo_no_diff_tracking(tmp_path: Path) -> None:
    """Without .git in cwd, files_changed should be empty and diff None."""
    adapter = ClaudeCodeAdapter()
    inv = AgentInvocation(role="r", prompt="p", cwd=tmp_path)
    fake = _fake_proc(stdout=_good_claude_blob("ok"), returncode=0)
    with patch(
        "adapters.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake),
    ):
        result = await adapter.execute(inv)
    assert result.files_changed == []
    assert result.diff is None
