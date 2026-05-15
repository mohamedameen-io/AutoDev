"""v0.31.0 (Phase 1.1): empty-result happy-path dump for both adapters.

Both Claude Code and Cursor adapters previously dumped to
``.autodev/debug/`` only on ``returncode != 0``. The "empty reviewer
response" failure mode (returncode 0, parseable JSON, ``result == ""``)
left no on-disk forensic artifact, which made root-cause analysis
guesswork. Phase 1.1 added an empty-result branch on the happy path
that writes ``.autodev/debug/<role>-<ts>-empty.json`` and gates the
behaviour behind ``AUTODEV_DEBUG_RAW_RESPONSES`` (default ``"1"``).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from adapters.claude_code import ClaudeCodeAdapter
from adapters.cursor import CursorAdapter
from adapters.types import AgentInvocation


def _fake_proc(
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
    pid: int = 12345,
) -> AsyncMock:
    proc = AsyncMock()
    proc.returncode = returncode
    proc.pid = pid
    proc.communicate = AsyncMock(
        return_value=(stdout.encode("utf-8"), stderr.encode("utf-8"))
    )
    proc.wait = AsyncMock(return_value=returncode)
    proc.kill = lambda: None
    return proc


def _empty_claude_blob() -> str:
    """Mimic the failure shape: rc=0, parseable JSON, empty result."""
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": 100,
            "num_turns": 1,
            "result": "",  # the empty-result happy path
            "stop_reason": "end_turn",
            "session_id": "00000000-0000-0000-0000-000000000000",
            "total_cost_usd": 0.0,
            "usage": {"input_tokens": 1, "output_tokens": 0},
            "modelUsage": {},
            "permission_denials": [],
            "terminal_reason": "completed",
            "uuid": "11111111-1111-1111-1111-111111111111",
        }
    )


def _empty_cursor_blob() -> str:
    return json.dumps({"result": "", "thread_id": "abc-123", "is_error": False})


# ----------------------------------------------------------------------------
# Claude Code adapter
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_empty_result_writes_debug_dump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rc=0 + empty result → ``.autodev/debug/<role>-*-empty.json``."""
    monkeypatch.setenv("AUTODEV_DEBUG_RAW_RESPONSES", "1")
    adapter = ClaudeCodeAdapter()
    inv = AgentInvocation(role="reviewer", prompt="p", cwd=tmp_path, max_turns=3)
    fake = _fake_proc(stdout=_empty_claude_blob(), returncode=0)
    with patch(
        "adapters.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake),
    ):
        result = await adapter.execute(inv)
    # Result should be flagged as a failure with the typed error.
    assert result.success is False
    assert result.error == "empty result from CLI"
    assert result.raw_stdout  # preserved
    # And a debug dump file should exist.
    debug_dir = tmp_path / ".autodev" / "debug"
    assert debug_dir.exists(), "debug dir should be created on demand"
    dumps = list(debug_dir.glob("reviewer-*-empty.json"))
    assert len(dumps) == 1, f"expected one empty-result dump, got {dumps}"
    payload = json.loads(dumps[0].read_text())
    assert payload["role"] == "reviewer"
    assert payload["raw_stdout"] == _empty_claude_blob()
    assert payload["prompt_size_bytes"] == len(b"p")
    assert "max_tokens" in payload["note"]


@pytest.mark.asyncio
async def test_claude_empty_result_dump_can_be_disabled_via_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``AUTODEV_DEBUG_RAW_RESPONSES=0`` skips the dump."""
    monkeypatch.setenv("AUTODEV_DEBUG_RAW_RESPONSES", "0")
    adapter = ClaudeCodeAdapter()
    inv = AgentInvocation(role="reviewer", prompt="p", cwd=tmp_path, max_turns=3)
    fake = _fake_proc(stdout=_empty_claude_blob(), returncode=0)
    with patch(
        "adapters.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake),
    ):
        result = await adapter.execute(inv)
    # Result still flagged as a failure (the env var only gates the dump).
    assert result.success is False
    debug_dir = tmp_path / ".autodev" / "debug"
    if debug_dir.exists():
        dumps = list(debug_dir.glob("reviewer-*-empty.json"))
        assert dumps == [], f"expected no empty-result dump, got {dumps}"


@pytest.mark.asyncio
async def test_claude_whitespace_only_result_treated_as_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whitespace-only ``result`` is indistinguishable from empty."""
    monkeypatch.setenv("AUTODEV_DEBUG_RAW_RESPONSES", "1")
    adapter = ClaudeCodeAdapter()
    inv = AgentInvocation(role="reviewer", prompt="p", cwd=tmp_path, max_turns=3)
    blob = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": 100,
            "num_turns": 1,
            "result": "   \n\t  ",
            "stop_reason": "end_turn",
            "session_id": "00000000-0000-0000-0000-000000000000",
            "total_cost_usd": 0.0,
            "usage": {},
            "modelUsage": {},
            "permission_denials": [],
            "terminal_reason": "completed",
            "uuid": "11111111-1111-1111-1111-111111111111",
        }
    )
    fake = _fake_proc(stdout=blob, returncode=0)
    with patch(
        "adapters.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake),
    ):
        result = await adapter.execute(inv)
    assert result.success is False
    assert result.error == "empty result from CLI"
    dumps = list((tmp_path / ".autodev" / "debug").glob("reviewer-*-empty.json"))
    assert len(dumps) == 1


@pytest.mark.asyncio
async def test_claude_non_empty_result_does_not_dump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A normal happy-path call must NOT produce a debug dump."""
    monkeypatch.setenv("AUTODEV_DEBUG_RAW_RESPONSES", "1")
    adapter = ClaudeCodeAdapter()
    inv = AgentInvocation(role="reviewer", prompt="p", cwd=tmp_path, max_turns=3)
    blob = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": 100,
            "num_turns": 1,
            "result": "VERDICT: APPROVED",
            "stop_reason": "end_turn",
            "session_id": "00000000-0000-0000-0000-000000000000",
            "total_cost_usd": 0.0,
            "usage": {},
            "modelUsage": {},
            "permission_denials": [],
            "terminal_reason": "completed",
            "uuid": "11111111-1111-1111-1111-111111111111",
        }
    )
    fake = _fake_proc(stdout=blob, returncode=0)
    with patch(
        "adapters.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake),
    ):
        result = await adapter.execute(inv)
    assert result.success is True
    debug_dir = tmp_path / ".autodev" / "debug"
    if debug_dir.exists():
        dumps = list(debug_dir.glob("reviewer-*-empty.json"))
        assert dumps == [], f"expected no dump on success, got {dumps}"


# ----------------------------------------------------------------------------
# Cursor adapter
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cursor_empty_result_writes_debug_dump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTODEV_DEBUG_RAW_RESPONSES", "1")
    adapter = CursorAdapter(binaries=("cursor",))
    inv = AgentInvocation(role="reviewer", prompt="p", cwd=tmp_path)
    fake = _fake_proc(stdout=_empty_cursor_blob(), returncode=0)
    with patch(
        "adapters.cursor.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake),
    ):
        result = await adapter.execute(inv)
    assert result.success is False
    assert result.error == "empty result from CLI"
    assert result.raw_stdout
    debug_dir = tmp_path / ".autodev" / "debug"
    assert debug_dir.exists()
    dumps = list(debug_dir.glob("reviewer-*-empty.json"))
    assert len(dumps) == 1, f"expected one empty-result dump, got {dumps}"
    payload = json.loads(dumps[0].read_text())
    assert payload["role"] == "reviewer"
    assert payload["raw_stdout"] == _empty_cursor_blob()


@pytest.mark.asyncio
async def test_cursor_empty_result_dump_can_be_disabled_via_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTODEV_DEBUG_RAW_RESPONSES", "0")
    adapter = CursorAdapter(binaries=("cursor",))
    inv = AgentInvocation(role="reviewer", prompt="p", cwd=tmp_path)
    fake = _fake_proc(stdout=_empty_cursor_blob(), returncode=0)
    with patch(
        "adapters.cursor.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake),
    ):
        result = await adapter.execute(inv)
    assert result.success is False
    debug_dir = tmp_path / ".autodev" / "debug"
    if debug_dir.exists():
        dumps = list(debug_dir.glob("reviewer-*-empty.json"))
        assert dumps == [], f"expected no dump when env disables it, got {dumps}"


@pytest.mark.asyncio
async def test_cursor_non_empty_result_does_not_dump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTODEV_DEBUG_RAW_RESPONSES", "1")
    adapter = CursorAdapter(binaries=("cursor",))
    inv = AgentInvocation(role="reviewer", prompt="p", cwd=tmp_path)
    blob = json.dumps(
        {"result": "VERDICT: APPROVED", "thread_id": "abc-123", "is_error": False}
    )
    fake = _fake_proc(stdout=blob, returncode=0)
    with patch(
        "adapters.cursor.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake),
    ):
        result = await adapter.execute(inv)
    assert result.success is True
    debug_dir = tmp_path / ".autodev" / "debug"
    if debug_dir.exists():
        dumps = list(debug_dir.glob("reviewer-*-empty.json"))
        assert dumps == [], f"expected no dump on success, got {dumps}"


# ----------------------------------------------------------------------------
# v0.31.1 (Phase 0): is_error=true AND result="" must still dump.
#
# v0.31.0 shipped the dump path with predicate ``not is_error and not text``,
# which silently skipped the dump whenever the CLI emitted ``is_error=true``
# alongside an empty ``result``. Per the v0.28.0 in-file comment, that is
# the dominant transport-failure shape — exactly the case the dump was
# built to capture. Drop the ``is_error`` gate; empty text is the
# machinery-failure signal regardless of whether ``is_error`` is set.
# ----------------------------------------------------------------------------


def _empty_claude_blob_with_is_error_true() -> str:
    """Mimic transport-layer failure: rc=0, parseable JSON, is_error=true,
    result="". The CLI exits cleanly but the underlying call failed, and
    the response body carries no recoverable text.
    """
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "duration_ms": 5000,
            "num_turns": 1,
            "result": "",
            "stop_reason": "max_tokens",
            "session_id": "00000000-0000-0000-0000-000000000000",
            "total_cost_usd": 0.0,
            "usage": {"input_tokens": 1000, "output_tokens": 0},
            "modelUsage": {},
            "permission_denials": [],
            "terminal_reason": "completed",
            "uuid": "11111111-1111-1111-1111-111111111111",
        }
    )


@pytest.mark.asyncio
async def test_claude_empty_result_with_is_error_true_still_dumps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for v0.31.1 Phase 0: when ``is_error=true`` AND
    ``result=""``, the dump must still fire. The v0.31.0 predicate
    silently skipped this branch — the most diagnostic case in
    production.
    """
    monkeypatch.setenv("AUTODEV_DEBUG_RAW_RESPONSES", "1")
    adapter = ClaudeCodeAdapter()
    inv = AgentInvocation(role="reviewer", prompt="p", cwd=tmp_path, max_turns=3)
    fake = _fake_proc(stdout=_empty_claude_blob_with_is_error_true(), returncode=0)
    with patch(
        "adapters.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake),
    ):
        result = await adapter.execute(inv)
    assert result.success is False
    dumps = list((tmp_path / ".autodev" / "debug").glob("reviewer-*-empty.json"))
    assert len(dumps) == 1, (
        "v0.31.1 regression: dump must fire even when CLI returns "
        f"is_error=true alongside an empty result; got dumps={dumps}"
    )


@pytest.mark.asyncio
async def test_cursor_empty_result_with_is_error_true_still_dumps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for v0.31.1 Phase 0: Cursor variant of the
    is_error=true + empty-result case.
    """
    monkeypatch.setenv("AUTODEV_DEBUG_RAW_RESPONSES", "1")
    adapter = CursorAdapter(binaries=("cursor",))
    inv = AgentInvocation(role="reviewer", prompt="p", cwd=tmp_path)
    blob = json.dumps(
        {
            "result": "",
            "thread_id": "abc-123",
            "is_error": True,
            "error": "transport timeout",
        }
    )
    fake = _fake_proc(stdout=blob, returncode=0)
    with patch(
        "adapters.cursor.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake),
    ):
        result = await adapter.execute(inv)
    assert result.success is False
    dumps = list((tmp_path / ".autodev" / "debug").glob("reviewer-*-empty.json"))
    assert len(dumps) == 1, (
        "v0.31.1 regression: cursor adapter dump must fire even when "
        f"CLI returns is_error=true alongside an empty result; got dumps={dumps}"
    )
