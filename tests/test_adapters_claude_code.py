"""v0.36.0 F2: Claude Code adapter network-probe retry behavior."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

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


# ---------------------------------------------------------------------------
# huge-repo Cluster B2: configurable + huge-repo-scaled probe timeout.
# ---------------------------------------------------------------------------


class _TimeoutCfg:
    """Minimal ``cfg.adapters`` stub carrying a probe_timeout_s knob."""

    probe_retry_attempts = 1
    probe_backoff_initial_s = 0.0

    def __init__(self, probe_timeout_s: float) -> None:
        self.probe_timeout_s = probe_timeout_s


def _capture_timeout_probe_once(captured: list[float]):
    """Return an async _pong_probe_once stand-in recording timeout_s."""

    async def _once(_self, _version_str, *, timeout_s: float = 10.0):
        captured.append(timeout_s)
        return True, "ok"

    return _once


@pytest.mark.asyncio
async def test_probe_timeout_default_20s_when_unbound() -> None:
    """No cfg bound → probe uses the 20s unbound default (v0.39.0).

    Raised from the legacy 10s so the detect-time probe (which runs on a
    throwaway, unbound adapter before ``get_adapter`` binds the cfg)
    survives a slow huge-repo cold start.
    """
    adapter = ClaudeCodeAdapter()
    captured: list[float] = []
    with patch.object(
        ClaudeCodeAdapter,
        "_pong_probe_once",
        new=_capture_timeout_probe_once(captured),
    ):
        ok, _ = await adapter._pong_probe(version_str="v1.0")
    assert ok is True
    assert captured == [20.0]


@pytest.mark.asyncio
async def test_probe_timeout_from_adapters_cfg() -> None:
    """probe_timeout_s=30 on the bound cfg → probe uses 30s."""
    adapter = ClaudeCodeAdapter()
    adapter.bind_adapters_cfg(_TimeoutCfg(probe_timeout_s=30.0))
    captured: list[float] = []
    with patch.object(
        ClaudeCodeAdapter,
        "_pong_probe_once",
        new=_capture_timeout_probe_once(captured),
    ):
        ok, _ = await adapter._pong_probe(version_str="v1.0")
    assert ok is True
    assert captured == [30.0]


@pytest.mark.asyncio
async def test_probe_timeout_huge_repo_scaled(monkeypatch) -> None:
    """_root_cfg/_probe_cwd bound + huge repo + 1.5x multiplier → 15s."""
    from config.defaults import default_config

    root_cfg = default_config()
    # Ensure the multiplier dict carries the probe_timeout_s key (schema
    # chunk 1 pre-populated it at 1.5).
    assert root_cfg.task_overrides.huge_repo_multipliers["probe_timeout_s"] == 1.5

    adapter = ClaudeCodeAdapter()
    adapter.bind_adapters_cfg(
        root_cfg.adapters, root_cfg=root_cfg, probe_cwd=Path("/tmp/huge")
    )

    # Force the huge-repo signal True regardless of the throwaway cwd.
    monkeypatch.setattr(
        "orchestrator.repo_size.is_huge_repo", lambda *a, **k: True
    )

    captured: list[float] = []
    with patch.object(
        ClaudeCodeAdapter,
        "_pong_probe_once",
        new=_capture_timeout_probe_once(captured),
    ):
        ok, _ = await adapter._pong_probe(version_str="v1.0")
    assert ok is True
    # base 10.0 * 1.5 = 15.0
    assert captured == [15.0]


@pytest.mark.asyncio
async def test_probe_timeout_resolver_raises_falls_back_to_base(monkeypatch) -> None:
    """Resolver raising → probe falls back to the base timeout, never crashes."""
    adapter = ClaudeCodeAdapter()
    adapter.bind_adapters_cfg(
        _TimeoutCfg(probe_timeout_s=22.0),
        root_cfg=object(),
        probe_cwd=Path("/tmp/huge"),
    )

    def _boom(*_a, **_k):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(
        "orchestrator.huge_repo_overrides.resolve_huge_repo_value", _boom
    )

    captured: list[float] = []
    with patch.object(
        ClaudeCodeAdapter,
        "_pong_probe_once",
        new=_capture_timeout_probe_once(captured),
    ):
        ok, _ = await adapter._pong_probe(version_str="v1.0")
    assert ok is True
    assert captured == [22.0]


# ---------------------------------------------------------------------------
# huge-repo Cluster B0: bind_adapters_cfg back-compat.
# ---------------------------------------------------------------------------


def test_bind_adapters_cfg_positional_backcompat() -> None:
    """Legacy positional single-arg call still works; new kwargs settable."""
    adapter = ClaudeCodeAdapter()
    # Defaults are None before binding.
    assert adapter._adapters_cfg is None
    assert adapter._root_cfg is None
    assert adapter._probe_cwd is None

    # Positional single-arg (the shape existing tests use) still works and
    # leaves the new fields untouched.
    cfg_block = _StubAdaptersCfg()
    adapter.bind_adapters_cfg(cfg_block)
    assert adapter._adapters_cfg is cfg_block
    assert adapter._root_cfg is None
    assert adapter._probe_cwd is None

    # New kwargs are settable.
    root = object()
    adapter.bind_adapters_cfg(cfg_block, root_cfg=root, probe_cwd=Path("/x"))
    assert adapter._root_cfg is root
    assert adapter._probe_cwd == Path("/x")


# ---------------------------------------------------------------------------
# huge-repo Cluster B1: spawn-agent isolation flags.
# ---------------------------------------------------------------------------

_ISOLATION = ["--setting-sources", "user", "--strict-mcp-config", "--mcp-config"]


def _make_inv(**overrides):
    from adapters.types import AgentInvocation

    base = dict(
        role="developer",
        prompt="do the thing",
        cwd=Path("/repo"),
        model="opus",
        allowed_tools=["Edit", "Bash"],
        max_turns=3,
    )
    base.update(overrides)
    return AgentInvocation(**base)


def test_build_command_includes_isolation_flags_by_default() -> None:
    """Default (suppress on) → cmd carries the 3 isolation flag groups."""
    adapter = ClaudeCodeAdapter()  # unbound → default True
    cmd = adapter._build_command(_make_inv())
    assert "--setting-sources" in cmd
    assert cmd[cmd.index("--setting-sources") + 1] == "user"
    assert "--strict-mcp-config" in cmd
    assert "--mcp-config" in cmd
    assert cmd[cmd.index("--mcp-config") + 1] == '{"mcpServers":{}}'


def test_build_command_omits_isolation_flags_when_suppress_false() -> None:
    """suppress_target_repo_config=False → flags absent (byte-identical)."""

    class _NoSuppress:
        suppress_target_repo_config = False

    inv = _make_inv()
    suppress_adapter = ClaudeCodeAdapter()
    suppress_adapter.bind_adapters_cfg(_NoSuppress())
    cmd_off = suppress_adapter._build_command(inv)

    assert "--setting-sources" not in cmd_off
    assert "--strict-mcp-config" not in cmd_off
    assert "--mcp-config" not in cmd_off

    # Snapshot: identical to the command a pre-isolation adapter would
    # build (i.e. the helper appended exactly nothing).
    expected = [
        "claude",
        "-p",
        inv.prompt,
        "--output-format",
        "json",
        "--permission-mode",
        "acceptEdits",
        "--model",
        "opus",
        "--max-turns",
        "3",
        "--allowed-tools",
        "Edit,Bash",
    ]
    assert cmd_off == expected


def test_probe_command_includes_isolation_flags_by_default() -> None:
    """The PONG probe command carries the isolation flags when suppress on."""
    captured_cmds: list[list[str]] = []

    def _fake_exec(*cmd, **_kw):
        captured_cmds.append(list(cmd))
        return _ok_proc()

    adapter = ClaudeCodeAdapter()  # unbound → default True

    import asyncio as _asyncio

    async def _run():
        with patch(
            "adapters.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=_fake_exec),
        ):
            await adapter._pong_probe_once("v1.0")

    _asyncio.run(_run())
    assert len(captured_cmds) == 1
    cmd = captured_cmds[0]
    assert "--setting-sources" in cmd
    assert "--strict-mcp-config" in cmd
    assert "--mcp-config" in cmd
    assert cmd[cmd.index("--mcp-config") + 1] == '{"mcpServers":{}}'


def test_probe_command_omits_isolation_flags_when_suppress_false() -> None:
    """suppress=False → probe command is the pre-isolation shape."""
    captured_cmds: list[list[str]] = []

    def _fake_exec(*cmd, **_kw):
        captured_cmds.append(list(cmd))
        return _ok_proc()

    class _NoSuppress:
        suppress_target_repo_config = False

    adapter = ClaudeCodeAdapter()
    adapter.bind_adapters_cfg(_NoSuppress())

    import asyncio as _asyncio

    async def _run():
        with patch(
            "adapters.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=_fake_exec),
        ):
            await adapter._pong_probe_once("v1.0")

    _asyncio.run(_run())
    # ``probe_model`` is independent of ``suppress_target_repo_config``:
    # the stub omits it, so the default "haiku" still pins ``--model``.
    # The isolation flags ARE absent (suppress=False).
    assert captured_cmds == [
        [
            "claude",
            "-p",
            "PONG",
            "--max-turns",
            "1",
            "--output-format",
            "json",
            "--model",
            "haiku",
        ]
    ]


# ---------------------------------------------------------------------------
# huge-repo follow-up: PONG probe pins a fast model.
# ---------------------------------------------------------------------------


def _capture_probe_cmd() -> tuple[list[list[str]], "callable"]:
    captured: list[list[str]] = []

    def _fake_exec(*cmd, **_kw):
        captured.append(list(cmd))
        return _ok_proc()

    return captured, _fake_exec


def test_probe_command_includes_default_haiku_model_when_unbound() -> None:
    """Unbound adapter → probe pins ``--model haiku`` (the default).

    The detect-time probe runs unbound, so the default MUST hold even
    without a cfg — that is exactly where the slow cold start hurts.
    """
    captured, _fake_exec = _capture_probe_cmd()
    adapter = ClaudeCodeAdapter()  # unbound

    import asyncio as _asyncio

    async def _run():
        with patch(
            "adapters.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=_fake_exec),
        ):
            await adapter._pong_probe_once("v1.0")

    _asyncio.run(_run())
    assert len(captured) == 1
    cmd = captured[0]
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "haiku"


def test_probe_command_respects_configured_probe_model() -> None:
    """``probe_model`` from the bound cfg overrides the default."""
    captured, _fake_exec = _capture_probe_cmd()

    class _ModelCfg:
        probe_model = "sonnet"

    adapter = ClaudeCodeAdapter()
    adapter.bind_adapters_cfg(_ModelCfg())

    import asyncio as _asyncio

    async def _run():
        with patch(
            "adapters.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=_fake_exec),
        ):
            await adapter._pong_probe_once("v1.0")

    _asyncio.run(_run())
    cmd = captured[0]
    assert cmd[cmd.index("--model") + 1] == "sonnet"


def test_probe_command_omits_model_when_probe_model_empty() -> None:
    """Empty ``probe_model`` → ``--model`` flag omitted (legacy default)."""
    captured, _fake_exec = _capture_probe_cmd()

    class _EmptyModelCfg:
        probe_model = ""

    adapter = ClaudeCodeAdapter()
    adapter.bind_adapters_cfg(_EmptyModelCfg())

    import asyncio as _asyncio

    async def _run():
        with patch(
            "adapters.claude_code.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=_fake_exec),
        ):
            await adapter._pong_probe_once("v1.0")

    _asyncio.run(_run())
    assert "--model" not in captured[0]


def test_default_probe_model_matches_schema_default() -> None:
    """The unbound-default probe model matches ``AdaptersConfig.probe_model``."""
    from adapters.claude_code import _DEFAULT_PROBE_MODEL
    from config.schema import AdaptersConfig

    assert _DEFAULT_PROBE_MODEL == AdaptersConfig().probe_model == "haiku"


# ---------------------------------------------------------------------------
# huge-repo Cluster B4: empty-result → infra subtype synthesis.
# ---------------------------------------------------------------------------


def _empty_result_proc(
    *, api_error_status=None, is_error=False, subtype="success"
) -> AsyncMock:
    blob: dict = {
        "type": "result",
        "is_error": is_error,
        "duration_ms": 10,
        "num_turns": 0,
        "result": "",
    }
    if subtype is not None:
        blob["subtype"] = subtype
    if api_error_status is not None:
        blob["api_error_status"] = api_error_status
    proc = AsyncMock()
    proc.returncode = 0
    proc.pid = 4321
    proc.communicate = AsyncMock(
        return_value=(json.dumps(blob).encode("utf-8"), b"")
    )
    proc.wait = AsyncMock(return_value=0)
    proc.kill = lambda: None
    return proc


@pytest.mark.asyncio
async def test_empty_result_with_429_sets_rate_limited(monkeypatch) -> None:
    """Empty result + api_error_status=429 → subtype synthesized rate_limited."""
    monkeypatch.setenv("AUTODEV_DEBUG_RAW_RESPONSES", "0")  # skip disk dump
    adapter = ClaudeCodeAdapter()
    inv = _make_inv()
    with patch(
        "adapters.claude_code.asyncio.create_subprocess_exec",
        new=AsyncMock(
            return_value=_empty_result_proc(api_error_status=429, is_error=True)
        ),
    ):
        result = await adapter.execute(inv)
    assert result.success is False
    assert result.error == "empty result from CLI"
    assert result.subtype == "rate_limited"
    assert result.api_error_status == 429


@pytest.mark.asyncio
async def test_empty_result_no_status_keeps_subtype_none(monkeypatch) -> None:
    """Genuine empty (no api_error_status) → subtype None (hard-fail preserved)."""
    monkeypatch.setenv("AUTODEV_DEBUG_RAW_RESPONSES", "0")
    adapter = ClaudeCodeAdapter()
    inv = _make_inv()
    with patch(
        "adapters.claude_code.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_empty_result_proc(subtype=None)),
    ):
        result = await adapter.execute(inv)
    assert result.success is False
    assert result.error == "empty result from CLI"
    # No CLI subtype + no infra status → subtype None (hard-fail preserved).
    assert result.subtype is None
    assert result.api_error_status is None
