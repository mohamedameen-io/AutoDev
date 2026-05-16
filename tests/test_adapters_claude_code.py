"""v0.36.0 F2: Claude Code adapter network-probe retry behavior."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from adapters.base import NetworkProbeFailure
from adapters.claude_code import ClaudeCodeAdapter


def _ok_blob() -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": 50,
            "num_turns": 1,
            "result": "PONG",
        }
    )


def _network_failure_proc() -> AsyncMock:
    proc = AsyncMock()
    proc.returncode = 0
    proc.pid = 1234
    proc.communicate = AsyncMock(
        return_value=(b"", b"connection reset by peer")
    )
    proc.wait = AsyncMock(return_value=0)
    proc.kill = lambda: None
    return proc


def _ok_proc() -> AsyncMock:
    proc = AsyncMock()
    proc.returncode = 0
    proc.pid = 1234
    proc.communicate = AsyncMock(return_value=(_ok_blob().encode("utf-8"), b""))
    proc.wait = AsyncMock(return_value=0)
    proc.kill = lambda: None
    return proc


class _StubAdaptersCfg:
    probe_retry_attempts = 3
    probe_backoff_initial_s = 0.0  # speed tests up; no real sleep


@pytest.mark.asyncio
async def test_pong_probe_retries_then_raises_structured() -> None:
    """Three failures → raise NetworkProbeFailure with .suggestion."""
    adapter = ClaudeCodeAdapter()
    adapter.bind_adapters_cfg(_StubAdaptersCfg())

    with patch(
        "adapters.claude_code.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=[_network_failure_proc() for _ in range(3)]),
    ):
        with pytest.raises(NetworkProbeFailure) as exc_info:
            await adapter._pong_probe(version_str="v1.0")
    exc = exc_info.value
    assert exc.adapter == "claude_code"
    assert exc.attempts == 3
    assert exc.suggestion  # non-empty remediation hint


@pytest.mark.asyncio
async def test_pong_probe_succeeds_on_retry_2() -> None:
    """Failure → failure → success returns (True, ...) without raising."""
    adapter = ClaudeCodeAdapter()
    adapter.bind_adapters_cfg(_StubAdaptersCfg())

    with patch(
        "adapters.claude_code.asyncio.create_subprocess_exec",
        new=AsyncMock(
            side_effect=[
                _network_failure_proc(),
                _network_failure_proc(),
                _ok_proc(),
            ]
        ),
    ):
        ok, msg = await adapter._pong_probe(version_str="v1.0")
    assert ok is True
    assert "v1.0" in msg or "ok" in msg


@pytest.mark.asyncio
async def test_pong_probe_auth_failure_short_circuits() -> None:
    """Auth failures do NOT retry — credentials won't fix themselves."""
    adapter = ClaudeCodeAdapter()
    adapter.bind_adapters_cfg(_StubAdaptersCfg())

    auth_blob = json.dumps(
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "duration_ms": 50,
            "num_turns": 0,
            "result": "Failed to authenticate. API Error: 401",
            "stop_reason": "error",
        }
    )
    auth_proc = AsyncMock()
    auth_proc.returncode = 0
    auth_proc.pid = 1234
    auth_proc.communicate = AsyncMock(
        return_value=(auth_blob.encode("utf-8"), b"")
    )
    auth_proc.wait = AsyncMock(return_value=0)
    auth_proc.kill = lambda: None

    # Single call — no retry — should land on (False, "auth_failed: ...").
    with patch(
        "adapters.claude_code.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=[auth_proc]),
    ):
        ok, msg = await adapter._pong_probe(version_str="v1.0")
    assert ok is False
    assert msg.startswith("auth_failed:")


# ---------------------------------------------------------------------------
# CLI catch-site: `autodev plan` exits 5 with rendered suggestion.
# ---------------------------------------------------------------------------


def test_cli_catches_network_probe_failure(tmp_path: Path) -> None:
    from cli import cli
    from config.defaults import default_config
    from config.loader import save_config

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        (cwd / ".autodev").mkdir(parents=True)
        save_config(default_config(), cwd / ".autodev" / "config.json")

        def _raise(*_a, **_kw):
            raise NetworkProbeFailure(
                adapter="claude_code",
                attempts=3,
                last_error="connection reset",
                suggestion="check VPN / proxy / adapter health",
            )

        with patch("cli.commands.plan.get_adapter", side_effect=_raise):
            result = runner.invoke(
                cli,
                ["plan", "--skip-spec-validation", "fix"],
                catch_exceptions=False,
            )
    assert result.exit_code == 5, result.output
    assert "network probe failed" in result.output.lower()
    assert "check vpn" in result.output.lower()
