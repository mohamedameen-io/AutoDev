"""Tests for the Cursor subprocess adapter (subprocess mocked)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from adapters.cursor import CursorAdapter
from adapters.types import AgentInvocation


def _fake_proc(
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
    hang: bool = False,
) -> AsyncMock:
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


def _good_cursor_blob(text: str = "PONG") -> str:
    # Cursor's JSON shape is less documented; exercise both "result" and
    # fallback keys in separate tests.
    return json.dumps(
        {
            "result": text,
            "thread_id": "abc-123",
            "is_error": False,
        }
    )


@pytest.mark.asyncio
async def test_execute_cursor_primary_binary(tmp_path: Path) -> None:
    adapter = CursorAdapter(binaries=("cursor", "cursor-agent"))
    inv = AgentInvocation(role="echo", prompt="hi", cwd=tmp_path)

    fake = _fake_proc(stdout=_good_cursor_blob("PONG"), returncode=0)
    spawn = AsyncMock(return_value=fake)
    with patch(
        "adapters.cursor.asyncio.create_subprocess_exec",
        spawn,
    ):
        result = await adapter.execute(inv)
    assert result.success is True
    assert result.text == "PONG"
    # First call should be against `cursor`.
    call = spawn.call_args_list[0]
    assert call.args[0] == "cursor"
    assert "agent" in call.args  # `cursor agent <prompt>` form


@pytest.mark.asyncio
async def test_execute_falls_back_to_cursor_agent(tmp_path: Path) -> None:
    adapter = CursorAdapter(binaries=("cursor", "cursor-agent"))
    inv = AgentInvocation(role="echo", prompt="hi", cwd=tmp_path)

    fake_ok = _fake_proc(stdout=_good_cursor_blob("PONG"), returncode=0)
    spawn = AsyncMock(side_effect=[FileNotFoundError("no cursor"), fake_ok])
    with patch(
        "adapters.cursor.asyncio.create_subprocess_exec",
        spawn,
    ):
        result = await adapter.execute(inv)
    assert result.success is True
    assert result.text == "PONG"
    assert spawn.call_count == 2
    # Second call should be against `cursor-agent`.
    second = spawn.call_args_list[1]
    assert second.args[0] == "cursor-agent"
    # cursor-agent form skips the "agent" subcommand.
    assert "agent" not in second.args[:2]


@pytest.mark.asyncio
async def test_execute_all_binaries_missing(tmp_path: Path) -> None:
    adapter = CursorAdapter(binaries=("a", "b"))
    inv = AgentInvocation(role="r", prompt="p", cwd=tmp_path)
    spawn = AsyncMock(side_effect=FileNotFoundError("nope"))
    with patch(
        "adapters.cursor.asyncio.create_subprocess_exec",
        spawn,
    ):
        result = await adapter.execute(inv)
    assert result.success is False
    assert result.error is not None
    assert "binary not found" in result.error or "not found" in result.error


@pytest.mark.asyncio
async def test_execute_cursor_timeout(tmp_path: Path) -> None:
    adapter = CursorAdapter(binaries=("cursor",))
    inv = AgentInvocation(role="r", prompt="p", cwd=tmp_path, timeout_s=1)
    fake = _fake_proc(hang=True)
    with patch(
        "adapters.cursor.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake),
    ):
        result = await adapter.execute(inv)
    assert result.success is False
    assert result.error is not None
    assert "timeout" in result.error.lower()


@pytest.mark.asyncio
async def test_execute_cursor_malformed_json(tmp_path: Path) -> None:
    adapter = CursorAdapter(binaries=("cursor",))
    inv = AgentInvocation(role="r", prompt="p", cwd=tmp_path)
    fake = _fake_proc(stdout="<html>not json</html>", returncode=0)
    with patch(
        "adapters.cursor.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake),
    ):
        result = await adapter.execute(inv)
    assert result.success is False
    assert result.error is not None
    assert "parse failed" in result.error
    assert result.raw_stdout == "<html>not json</html>"


@pytest.mark.asyncio
async def test_execute_cursor_nonzero_exit(tmp_path: Path) -> None:
    adapter = CursorAdapter(binaries=("cursor",))
    inv = AgentInvocation(role="r", prompt="p", cwd=tmp_path)
    fake = _fake_proc(stdout="", stderr="not logged in", returncode=3)
    with patch(
        "adapters.cursor.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake),
    ):
        result = await adapter.execute(inv)
    assert result.success is False
    assert result.error is not None
    assert "not logged in" in result.error


@pytest.mark.asyncio
async def test_execute_cursor_text_fallback_keys(tmp_path: Path) -> None:
    """Cursor shape drift: ensure we can pull text from 'response' fallback."""
    adapter = CursorAdapter(binaries=("cursor",))
    inv = AgentInvocation(role="r", prompt="p", cwd=tmp_path)
    blob = json.dumps({"response": "hello via response", "is_error": False})
    fake = _fake_proc(stdout=blob, returncode=0)
    with patch(
        "adapters.cursor.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake),
    ):
        result = await adapter.execute(inv)
    assert result.success is True
    assert result.text == "hello via response"


@pytest.mark.asyncio
async def test_allowed_tools_warning_logged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Structlog is configured with PrintLoggerFactory -> writes to stdout."""
    adapter = CursorAdapter(binaries=("cursor",))
    inv = AgentInvocation(
        role="r",
        prompt="p",
        cwd=tmp_path,
        allowed_tools=["Read", "Edit"],
    )
    fake = _fake_proc(stdout=_good_cursor_blob("ok"), returncode=0)
    with patch(
        "adapters.cursor.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake),
    ):
        await adapter.execute(inv)
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "cursor.allowed_tools_ignored" in combined
    assert "allowed_tools" in combined.lower()
    # Ensure it's actually at warning level.
    assert "warning" in combined.lower()


@pytest.mark.asyncio
async def test_cursor_healthcheck_success() -> None:
    adapter = CursorAdapter(binaries=("cursor",))
    fake = _fake_proc(stdout="cursor 0.42.0\n", returncode=0)
    with patch(
        "adapters.cursor.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake),
    ):
        ok, details = await adapter.healthcheck()
    assert ok is True
    assert "0.42.0" in details


@pytest.mark.asyncio
async def test_cursor_healthcheck_falls_back_to_cursor_agent() -> None:
    adapter = CursorAdapter(binaries=("cursor", "cursor-agent"))
    # First binary missing, second works.
    fake_ok = _fake_proc(stdout="cursor-agent 0.1.0\n", returncode=0)
    spawn = AsyncMock(side_effect=[FileNotFoundError("nope"), fake_ok])
    with patch(
        "adapters.cursor.asyncio.create_subprocess_exec",
        spawn,
    ):
        ok, details = await adapter.healthcheck()
    assert ok is True
    assert "cursor-agent" in details


@pytest.mark.asyncio
async def test_cursor_healthcheck_all_fail() -> None:
    adapter = CursorAdapter(binaries=("cursor", "cursor-agent"))
    spawn = AsyncMock(side_effect=FileNotFoundError("missing"))
    with patch(
        "adapters.cursor.asyncio.create_subprocess_exec",
        spawn,
    ):
        ok, details = await adapter.healthcheck()
    assert ok is False
    assert "not found" in details


@pytest.mark.asyncio
async def test_cursor_init_workspace_is_stub(tmp_path: Path) -> None:
    adapter = CursorAdapter()
    await adapter.init_workspace(tmp_path, [])
