"""Tests for platform auto-detection precedence."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from adapters.claude_code import ClaudeCodeAdapter
from adapters.cursor import CursorAdapter
from adapters.detect import detect_platform, get_adapter
from errors import AdapterError


@pytest.mark.asyncio
async def test_preferred_claude_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTODEV_PLATFORM", raising=False)
    with patch.object(
        ClaudeCodeAdapter, "healthcheck", AsyncMock(return_value=(True, "ok"))
    ):
        name = await detect_platform("claude_code")
    assert name == "claude_code"


@pytest.mark.asyncio
async def test_preferred_claude_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AUTODEV_PLATFORM", raising=False)
    with patch.object(
        ClaudeCodeAdapter,
        "healthcheck",
        AsyncMock(return_value=(False, "binary missing")),
    ):
        with pytest.raises(AdapterError, match="unavailable"):
            await detect_platform("claude_code")


@pytest.mark.asyncio
async def test_preferred_cursor_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTODEV_PLATFORM", raising=False)
    with patch.object(
        CursorAdapter, "healthcheck", AsyncMock(return_value=(True, "ok"))
    ):
        name = await detect_platform("cursor")
    assert name == "cursor"


@pytest.mark.asyncio
async def test_env_var_overrides_in_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTODEV_PLATFORM", "cursor")
    with patch.object(
        CursorAdapter, "healthcheck", AsyncMock(return_value=(True, "ok"))
    ):
        name = await detect_platform("auto")
    assert name == "cursor"


@pytest.mark.asyncio
async def test_env_var_invalid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTODEV_PLATFORM", "chatgpt")
    with pytest.raises(AdapterError, match="invalid"):
        await detect_platform("auto")


@pytest.mark.asyncio
async def test_env_var_set_but_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTODEV_PLATFORM", "claude_code")
    with patch.object(
        ClaudeCodeAdapter,
        "healthcheck",
        AsyncMock(return_value=(False, "binary missing")),
    ):
        with pytest.raises(AdapterError, match="unavailable"):
            await detect_platform("auto")


@pytest.mark.asyncio
async def test_auto_prefers_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTODEV_PLATFORM", raising=False)
    with (
        patch.object(
            ClaudeCodeAdapter,
            "healthcheck",
            AsyncMock(return_value=(True, "claude ok")),
        ),
        patch.object(
            CursorAdapter,
            "healthcheck",
            AsyncMock(return_value=(True, "cursor ok")),
        ),
    ):
        name = await detect_platform("auto")
    assert name == "claude_code"


@pytest.mark.asyncio
async def test_auto_falls_back_to_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AUTODEV_PLATFORM", raising=False)
    with (
        patch.object(
            ClaudeCodeAdapter,
            "healthcheck",
            AsyncMock(return_value=(False, "no claude")),
        ),
        patch.object(
            CursorAdapter,
            "healthcheck",
            AsyncMock(return_value=(True, "cursor ok")),
        ),
    ):
        name = await detect_platform("auto")
    assert name == "cursor"


@pytest.mark.asyncio
async def test_auto_neither_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AUTODEV_PLATFORM", raising=False)
    with (
        patch.object(
            ClaudeCodeAdapter,
            "healthcheck",
            AsyncMock(return_value=(False, "no claude")),
        ),
        patch.object(
            CursorAdapter,
            "healthcheck",
            AsyncMock(return_value=(False, "no cursor")),
        ),
    ):
        with pytest.raises(AdapterError, match="No platform CLI"):
            await detect_platform("auto")


@pytest.mark.asyncio
async def test_invalid_preferred_name() -> None:
    with pytest.raises(AdapterError, match="invalid"):
        await detect_platform("windsurf")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_adapter_returns_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AUTODEV_PLATFORM", raising=False)
    with patch.object(
        ClaudeCodeAdapter, "healthcheck", AsyncMock(return_value=(True, "ok"))
    ):
        adapter = await get_adapter("claude_code")
    assert isinstance(adapter, ClaudeCodeAdapter)


@pytest.mark.asyncio
async def test_get_adapter_auto_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AUTODEV_PLATFORM", raising=False)
    with (
        patch.object(
            ClaudeCodeAdapter,
            "healthcheck",
            AsyncMock(return_value=(False, "no")),
        ),
        patch.object(
            CursorAdapter,
            "healthcheck",
            AsyncMock(return_value=(True, "cursor ok")),
        ),
    ):
        adapter = await get_adapter("auto")
    assert isinstance(adapter, CursorAdapter)
