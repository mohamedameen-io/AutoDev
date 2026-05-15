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
    # Workspace Trust bypass: recent Cursor Agent versions refuse to run in
    # an untrusted directory unless `--force` (or `-f`/`--yolo`) is passed.
    assert "--force" in call.args


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
    # Workspace Trust bypass must be present on the fallback form too.
    assert "--force" in second.args


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
async def test_execute_default_timeout_when_none(tmp_path: Path) -> None:
    """``inv.timeout_s=None`` must not crash ``asyncio.wait_for``.

    Regression for v0.30.1 Bug F2: passing ``None`` as the second positional
    arg to ``asyncio.wait_for`` is technically valid (it disables the
    timeout), but we used to interpolate the same ``None`` into the timeout
    error message and feed it to circuit-breaker arithmetic. Mirror the
    claude_code adapter's 600s fallback.
    """
    adapter = CursorAdapter(binaries=("cursor",))
    inv = AgentInvocation(role="r", prompt="p", cwd=tmp_path, timeout_s=None)
    fake = _fake_proc(stdout=_good_cursor_blob("PONG"), returncode=0)
    with patch(
        "adapters.cursor.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake),
    ):
        # Must not raise TypeError on ``None`` arithmetic.
        result = await adapter.execute(inv)
    assert result.success is True
    assert result.text == "PONG"


@pytest.mark.asyncio
async def test_execute_default_timeout_message_does_not_say_none(
    tmp_path: Path,
) -> None:
    """When timeout_s is None and a timeout fires, the error message must
    show the resolved default (600s), not the literal string 'None'."""
    adapter = CursorAdapter(binaries=("cursor",))
    inv = AgentInvocation(role="r", prompt="p", cwd=tmp_path, timeout_s=None)
    fake = _fake_proc(hang=True)
    with patch(
        "adapters.cursor.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake),
    ), patch(
        "adapters.cursor.asyncio.wait_for",
        AsyncMock(side_effect=asyncio.TimeoutError()),
    ):
        result = await adapter.execute(inv)
    assert result.success is False
    assert result.error is not None
    assert "None" not in result.error
    assert "600" in result.error


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


# ---------------------------------------------------------------------------
# v0.31.0 (Phase 2.6): usage-limit detection + always-fall-back-to-auto-with-
# Max-Mode-disabled.
# ---------------------------------------------------------------------------


def test_classify_limit_signal_categorises_correctly() -> None:
    """Unit-test the limit-signal classifier across wording variants."""
    from adapters.cursor import _classify_limit_signal

    # Plain rate-limit phrasing -> rate_limited
    assert _classify_limit_signal("", "rate limit hit", 0) == "rate_limited"
    assert _classify_limit_signal("", "RATE_LIMIT exceeded", 0) == "rate_limited"
    assert (
        _classify_limit_signal("", "too many requests", 0) == "rate_limited"
    )

    # 429 returncode is unambiguous (defaults to rate_limited unless the
    # message refines to usage-cap wording).
    assert _classify_limit_signal("", "", 429) == "rate_limited"

    # Usage-cap wording variants -> usage_limit_hit
    assert (
        _classify_limit_signal("", "you have hit your usage limit", 0)
        == "usage_limit_hit"
    )
    assert (
        _classify_limit_signal("", "monthly limit reached", 0)
        == "usage_limit_hit"
    )
    assert (
        _classify_limit_signal("", "plan limit exceeded", 0)
        == "usage_limit_hit"
    )
    assert (
        _classify_limit_signal("", "quota exceeded for this account", 0)
        == "usage_limit_hit"
    )
    assert (
        _classify_limit_signal("", "you are out of credits", 0)
        == "usage_limit_hit"
    )
    assert (
        _classify_limit_signal("", "upgrade to continue using cursor", 0)
        == "usage_limit_hit"
    )
    assert _classify_limit_signal("", "limit reached", 0) == "usage_limit_hit"

    # Backend sometimes returns the message in JSON on stdout instead of stderr.
    assert (
        _classify_limit_signal('{"error":"usage limit"}', "", 0)
        == "usage_limit_hit"
    )

    # 429 + usage wording -> usage_limit_hit (refinement)
    assert (
        _classify_limit_signal("", "usage limit reached", 429)
        == "usage_limit_hit"
    )

    # Genuine non-limit failure -> none
    assert _classify_limit_signal("", "not logged in", 1) == "none"
    assert _classify_limit_signal("", "", 0) == "none"


@pytest.mark.asyncio
async def test_usage_limit_triggers_downshift_to_auto_max_off(
    tmp_path: Path,
) -> None:
    """Initial sonnet attempt hits a usage limit; the next call must use
    ``--model auto`` and have ``max_mode=False`` (Phase 2.6 policy).
    """
    adapter = CursorAdapter(binaries=("cursor",))
    inv = AgentInvocation(
        role="developer", prompt="hi", cwd=tmp_path, model="sonnet"
    )

    fake_limit = _fake_proc(
        stdout="", stderr="usage limit reached for this month", returncode=1
    )
    fake_ok = _fake_proc(stdout=_good_cursor_blob("PONG"), returncode=0)
    spawn = AsyncMock(side_effect=[fake_limit, fake_ok])
    with patch(
        "adapters.cursor.asyncio.create_subprocess_exec",
        spawn,
    ):
        result = await adapter.execute(inv)

    assert result.success is True
    assert result.text == "PONG"
    assert spawn.call_count == 2

    first_args = spawn.call_args_list[0].args
    second_args = spawn.call_args_list[1].args
    # First attempt: original sonnet model.
    assert "--model" in first_args
    assert first_args[first_args.index("--model") + 1] == "sonnet"
    # Second attempt: downshifted to auto.
    assert "--model" in second_args
    assert second_args[second_args.index("--model") + 1] == "auto"


@pytest.mark.asyncio
async def test_rate_limit_also_downshifts_when_starting_from_auto(
    tmp_path: Path,
) -> None:
    """Even when ``inv.model is None``, a usage-limit signal triggers the
    one-shot downshift (Phase 2.6.4 — drop the narrow opus/sonnet
    precondition)."""
    adapter = CursorAdapter(binaries=("cursor",))
    inv = AgentInvocation(role="reviewer", prompt="hi", cwd=tmp_path)
    assert inv.model is None  # sanity check the new path

    fake_limit = _fake_proc(
        stdout="", stderr="usage limit hit", returncode=1
    )
    fake_ok = _fake_proc(stdout=_good_cursor_blob("OK"), returncode=0)
    spawn = AsyncMock(side_effect=[fake_limit, fake_ok])
    with patch(
        "adapters.cursor.asyncio.create_subprocess_exec",
        spawn,
    ):
        result = await adapter.execute(inv)

    assert result.success is True
    assert spawn.call_count == 2
    # The downshift must have set --model auto on the retry, even though
    # the original invocation had no model set at all.
    second_args = spawn.call_args_list[1].args
    assert "--model" in second_args
    assert second_args[second_args.index("--model") + 1] == "auto"


@pytest.mark.asyncio
async def test_only_one_downshift_per_call_to_avoid_loops(
    tmp_path: Path,
) -> None:
    """Two consecutive usage-limit signals must produce exactly two attempts
    (original + one downshift). The third call never happens — the adapter
    surfaces the error rather than looping."""
    adapter = CursorAdapter(binaries=("cursor",))
    inv = AgentInvocation(
        role="developer", prompt="hi", cwd=tmp_path, model="sonnet"
    )

    fake_limit_1 = _fake_proc(
        stdout="", stderr="usage limit reached", returncode=1
    )
    fake_limit_2 = _fake_proc(
        stdout="", stderr="usage limit reached again", returncode=1
    )
    # A third response is provided to assert it is NOT consumed.
    fake_ok = _fake_proc(stdout=_good_cursor_blob("never"), returncode=0)
    spawn = AsyncMock(side_effect=[fake_limit_1, fake_limit_2, fake_ok])
    with patch(
        "adapters.cursor.asyncio.create_subprocess_exec",
        spawn,
    ):
        result = await adapter.execute(inv)

    assert result.success is False
    # Exactly two attempts: original (sonnet) and the one downshift (auto).
    assert spawn.call_count == 2
    assert result.subtype == "usage_limit_hit"
    assert result.error is not None
    assert "usage_limit_hit" in result.error


@pytest.mark.asyncio
async def test_disable_fallback_env_var_short_circuits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``AUTODEV_CURSOR_DISABLE_MAX_FALLBACK=1`` must skip the downshift
    entirely and surface the underlying error on the first hit."""
    monkeypatch.setenv("AUTODEV_CURSOR_DISABLE_MAX_FALLBACK", "1")
    adapter = CursorAdapter(binaries=("cursor",))
    inv = AgentInvocation(
        role="developer", prompt="hi", cwd=tmp_path, model="sonnet"
    )

    fake_limit = _fake_proc(
        stdout="", stderr="usage limit reached", returncode=1
    )
    fake_ok = _fake_proc(stdout=_good_cursor_blob("never"), returncode=0)
    spawn = AsyncMock(side_effect=[fake_limit, fake_ok])
    with patch(
        "adapters.cursor.asyncio.create_subprocess_exec",
        spawn,
    ):
        result = await adapter.execute(inv)

    assert result.success is False
    # Exactly one call — the downshift never fires.
    assert spawn.call_count == 1
    assert result.subtype == "usage_limit_hit"


def test_usage_limit_subtype_feeds_circuit_breaker() -> None:
    """The new ``usage_limit_hit`` subtype must be in the circuit
    breaker's tracked-subtypes set so a sustained stream of caps halts
    the run."""
    from orchestrator.circuit_breaker import (
        INFRASTRUCTURE_SUBTYPES,
        InfraFailureCircuitBreaker,
    )
    import datetime as _dt

    assert "usage_limit_hit" in INFRASTRUCTURE_SUBTYPES

    cb = InfraFailureCircuitBreaker(threshold=3, window_s=60.0)
    base = _dt.datetime(2026, 5, 13, 12, 0, 0, tzinfo=_dt.timezone.utc)
    cb.record_failure("t1", "usage_limit_hit", base)
    cb.record_failure(
        "t2", "usage_limit_hit", base + _dt.timedelta(seconds=10)
    )
    cb.record_failure(
        "t3", "usage_limit_hit", base + _dt.timedelta(seconds=20)
    )
    halt, reason = cb.should_halt()
    assert halt is True
    assert reason is not None
