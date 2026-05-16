"""Tests for the Claude Code adapter's PONG-probe healthcheck (Bug 10).

The healthcheck contract is two-stage:
  1. Cheap ``claude --version`` (catches "CLI missing").
  2. Live ``echo PONG | claude -p --max-turns 1`` (catches auth/network).

When stage 2 returns ``is_error=true`` with HTTP 401/403 in the message, the
healthcheck must surface ``(False, "auth_failed: ...")``. On timeout / network
errors the prefix becomes ``"network: ..."``. On success, ``(True, ...)``.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from adapters.claude_code import ClaudeCodeAdapter


def _fake_proc(
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
    hang: bool = False,
) -> AsyncMock:
    """AsyncMock mimicking ``asyncio.subprocess.Process`` for healthcheck."""
    proc = AsyncMock()
    proc.returncode = returncode
    proc.pid = 4242
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


def _auth_failed_blob(status: int = 403) -> str:
    """Synthetic claude -p JSON for an auth failure."""
    return json.dumps(
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "duration_ms": 50,
            "num_turns": 0,
            "result": (
                f"Failed to authenticate. API Error: {status} "
                "{\"type\":\"error\",\"error\":{\"type\":\"authentication_error\"}}"
            ),
            "stop_reason": "error",
            "session_id": "00000000-0000-0000-0000-000000000000",
            "total_cost_usd": 0.0,
            "usage": {},
            "modelUsage": {},
            "permission_denials": [],
            "terminal_reason": "errored",
            "uuid": "11111111-1111-1111-1111-111111111111",
        }
    )


def _pong_blob() -> str:
    """Synthetic claude -p JSON for a successful PONG round-trip."""
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": 80,
            "num_turns": 1,
            "result": "PONG",
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
async def test_healthcheck_returns_auth_failed_on_403() -> None:
    """When the PONG probe returns is_error=true with 403, surface auth_failed."""
    adapter = ClaudeCodeAdapter()
    version_proc = _fake_proc(stdout="2.1.92 (Claude Code)\n", returncode=0)
    pong_proc = _fake_proc(stdout=_auth_failed_blob(403), returncode=0)

    # First call = --version, second call = -p PONG.
    with patch(
        "adapters.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(side_effect=[version_proc, pong_proc]),
    ):
        ok, details = await adapter.healthcheck()

    assert ok is False
    assert details.startswith("auth_failed:")
    assert "403" in details


@pytest.mark.asyncio
async def test_healthcheck_returns_ok_on_pong() -> None:
    """Both --version and PONG succeed → (True, _)."""
    adapter = ClaudeCodeAdapter()
    version_proc = _fake_proc(stdout="2.1.92 (Claude Code)\n", returncode=0)
    pong_proc = _fake_proc(stdout=_pong_blob(), returncode=0)

    with patch(
        "adapters.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(side_effect=[version_proc, pong_proc]),
    ):
        ok, details = await adapter.healthcheck()

    assert ok is True
    assert "2.1.92" in details or "PONG" in details or details


@pytest.mark.asyncio
async def test_healthcheck_returns_network_on_timeout() -> None:
    """v0.36.0 F2: PONG hangs past deadline → after retries, the probe
    raises :class:`NetworkProbeFailure`. The legacy ``(False,
    "network:...")`` shape now lives on ``exc.last_error`` for callers
    that haven't migrated.
    """
    from adapters.base import NetworkProbeFailure

    adapter = ClaudeCodeAdapter()

    # Stub adapters cfg so backoff is instant (no real sleep).
    class _Cfg:
        probe_retry_attempts = 3
        probe_backoff_initial_s = 0.0

    adapter.bind_adapters_cfg(_Cfg())

    version_proc = _fake_proc(stdout="2.1.92 (Claude Code)\n", returncode=0)
    pong_procs = [_fake_proc(hang=True) for _ in range(3)]

    with patch(
        "adapters.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(side_effect=[version_proc, *pong_procs]),
    ):
        # Patch wait_for so the test doesn't actually sleep 10s.
        original_wait_for = asyncio.wait_for
        call_idx = {"n": 0}

        async def _fake_wait_for(coro, timeout):
            call_idx["n"] += 1
            if call_idx["n"] == 1:
                # Let --version drain normally.
                return await original_wait_for(coro, timeout=2)
            # Force timeout on every PONG attempt (and close the coro
            # to avoid "coroutine was never awaited" warnings).
            coro.close()
            raise asyncio.TimeoutError

        with patch(
            "adapters.claude_code.asyncio.wait_for",
            side_effect=_fake_wait_for,
        ):
            with pytest.raises(NetworkProbeFailure) as exc_info:
                await adapter.healthcheck()

    exc = exc_info.value
    assert exc.attempts == 3
    assert "network:" in exc.last_error


@pytest.mark.asyncio
async def test_healthcheck_version_succeeds_but_pong_403_returns_auth_failed() -> None:
    """Stage 1 (--version) ok, stage 2 (PONG) auth_failed → (False, auth_failed: ...).

    This is the regression case: a working CLI binary masking expired auth.
    """
    adapter = ClaudeCodeAdapter()
    version_proc = _fake_proc(stdout="2.1.92 (Claude Code)\n", returncode=0)
    pong_proc = _fake_proc(stdout=_auth_failed_blob(401), returncode=0)

    with patch(
        "adapters.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(side_effect=[version_proc, pong_proc]),
    ):
        ok, details = await adapter.healthcheck()

    assert ok is False
    assert details.startswith("auth_failed:")
    assert "401" in details
