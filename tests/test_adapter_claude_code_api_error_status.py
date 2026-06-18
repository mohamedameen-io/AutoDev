"""Tests for ``api_error_status`` → ``subtype`` synthesis in the Claude
Code adapter (Bug 1, v0.28.0).

The CLI surfaces transport-layer HTTP failures as JSON payloads with
``is_error=true`` and a typed ``api_error_status`` integer (e.g. 403 for
auth failure, 429 for rate-limit, 5xx for server). Pre-v0.28 the adapter
only mapped ``parsed.get("subtype")``, so a 403 disappeared into a
free-text ``error`` string and the tournament classifier never saw a
typed signal. These tests pin the new mapping behavior:

    401, 403       → ``auth_failed``
    429            → ``rate_limited``
    500-599        → ``server_error``
    400-499 (other)→ ``client_error``

The CLI's own ``subtype`` (e.g. ``error_max_turns``) always wins when
present — synthesis is a fallback, never an override.
"""

from __future__ import annotations

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
    pid: int = 12345,
) -> AsyncMock:
    """AsyncMock mimicking ``asyncio.subprocess.Process``."""
    proc = AsyncMock()
    proc.returncode = returncode
    proc.pid = pid
    proc.communicate = AsyncMock(
        return_value=(stdout.encode("utf-8"), stderr.encode("utf-8"))
    )
    proc.wait = AsyncMock(return_value=returncode)
    proc.kill = lambda: None
    return proc


@pytest.mark.asyncio
async def test_403_response_sets_auth_failed_subtype(tmp_path: Path) -> None:
    """rc=1 + JSON body with ``api_error_status=403`` → ``subtype="auth_failed"``."""
    adapter = ClaudeCodeAdapter()
    inv = AgentInvocation(role="r", prompt="p", cwd=tmp_path, max_turns=1)
    blob = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "api_error_status": 403,
            "result": "Failed to authenticate. API Error: 403",
            "duration_ms": 520,
            "duration_api_ms": 0,
        }
    )
    fake = _fake_proc(stdout=blob, stderr="", returncode=1)
    with patch(
        "adapters.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake),
    ):
        result = await adapter.execute(inv)
    assert result.success is False
    assert result.subtype == "auth_failed"
    assert result.api_error_status == 403


@pytest.mark.asyncio
async def test_429_response_sets_rate_limited_subtype(tmp_path: Path) -> None:
    """``api_error_status=429`` → ``subtype="rate_limited"``."""
    adapter = ClaudeCodeAdapter()
    inv = AgentInvocation(role="r", prompt="p", cwd=tmp_path, max_turns=1)
    blob = json.dumps(
        {
            "type": "result",
            "is_error": True,
            "api_error_status": 429,
            "result": "Too Many Requests",
            "duration_ms": 100,
            "duration_api_ms": 0,
        }
    )
    fake = _fake_proc(stdout=blob, stderr="", returncode=1)
    with patch(
        "adapters.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake),
    ):
        result = await adapter.execute(inv)
    assert result.success is False
    assert result.subtype == "rate_limited"
    assert result.api_error_status == 429


@pytest.mark.asyncio
async def test_503_response_sets_server_error_subtype(tmp_path: Path) -> None:
    """``api_error_status=503`` → ``subtype="server_error"``."""
    adapter = ClaudeCodeAdapter()
    inv = AgentInvocation(role="r", prompt="p", cwd=tmp_path, max_turns=1)
    blob = json.dumps(
        {
            "type": "result",
            "is_error": True,
            "api_error_status": 503,
            "result": "Service Unavailable",
            "duration_ms": 200,
            "duration_api_ms": 0,
        }
    )
    fake = _fake_proc(stdout=blob, stderr="", returncode=1)
    with patch(
        "adapters.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake),
    ):
        result = await adapter.execute(inv)
    assert result.success is False
    assert result.subtype == "server_error"
    assert result.api_error_status == 503


@pytest.mark.asyncio
async def test_existing_subtype_takes_precedence(tmp_path: Path) -> None:
    """When the CLI provides its own ``subtype``, synthesis is suppressed.

    The CLI's classification (e.g. ``error_max_turns``) is more specific
    than the synthesized HTTP-class; it must win.
    """
    adapter = ClaudeCodeAdapter()
    inv = AgentInvocation(role="r", prompt="p", cwd=tmp_path, max_turns=1)
    blob = json.dumps(
        {
            "type": "result",
            "subtype": "error_max_turns",
            "is_error": True,
            "api_error_status": 429,
            "result": "",
            "duration_ms": 100,
            "duration_api_ms": 0,
        }
    )
    fake = _fake_proc(stdout=blob, stderr="", returncode=1)
    with patch(
        "adapters.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake),
    ):
        result = await adapter.execute(inv)
    assert result.success is False
    assert result.subtype == "error_max_turns"
    # api_error_status is still surfaced for ledger logging even when
    # the CLI's own subtype wins the precedence battle.
    assert result.api_error_status == 429


@pytest.mark.asyncio
async def test_no_api_error_status_no_subtype_synthesis(tmp_path: Path) -> None:
    """``is_error=true`` without ``api_error_status`` → no synthesis."""
    adapter = ClaudeCodeAdapter()
    inv = AgentInvocation(role="r", prompt="p", cwd=tmp_path, max_turns=1)
    blob = json.dumps(
        {
            "type": "result",
            "is_error": True,
            "result": "some opaque failure",
            "duration_ms": 100,
            "duration_api_ms": 0,
        }
    )
    fake = _fake_proc(stdout=blob, stderr="", returncode=0)
    with patch(
        "adapters.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake),
    ):
        result = await adapter.execute(inv)
    assert result.success is False
    assert result.subtype is None
    assert result.api_error_status is None


@pytest.mark.asyncio
async def test_rc_zero_with_is_error_true_still_synthesizes(
    tmp_path: Path,
) -> None:
    """The CLI sometimes exits 0 yet writes ``is_error=true`` in the body.

    The success-path branch must run the same synthesis as the rc!=0
    branch — so a 403 reported via rc=0 still surfaces ``auth_failed``.
    """
    adapter = ClaudeCodeAdapter()
    inv = AgentInvocation(role="r", prompt="p", cwd=tmp_path, max_turns=1)
    blob = json.dumps(
        {
            "type": "result",
            "is_error": True,
            "api_error_status": 403,
            "result": "Failed to authenticate. API Error: 403",
            "duration_ms": 120,
            "duration_api_ms": 0,
        }
    )
    fake = _fake_proc(stdout=blob, stderr="", returncode=0)
    with patch(
        "adapters.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake),
    ):
        result = await adapter.execute(inv)
    assert result.success is False
    assert result.subtype == "auth_failed"
    assert result.api_error_status == 403
