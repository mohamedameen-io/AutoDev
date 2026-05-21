"""Tests for platform auto-detection precedence."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from adapters.claude_code import ClaudeCodeAdapter
from adapters.cursor import CursorAdapter
from adapters.detect import detect_platform, get_adapter
from errors import AdapterError


# v0.37.0 H4: ensure trigger-context env vars (Claude Code / Cursor
# host signals) don't bleed in from the developer shell and pre-empt
# the AUTODEV_PLATFORM-env / fitness / fallback precedence these tests
# assert. The trigger-context path has dedicated coverage in
# ``test_adapter_detect_trigger_context.py``.
@pytest.fixture(autouse=True)
def _clear_trigger_context_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    for key in [k for k in list(__import__("os").environ) if k.startswith("CURSOR_")]:
        monkeypatch.delenv(key, raising=False)


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
        # v0.38.0 HK10: get_adapter now returns (adapter, selection_meta).
        adapter, selection_meta = await get_adapter("claude_code")
    assert isinstance(adapter, ClaudeCodeAdapter)
    assert selection_meta["platform"] == "claude_code"
    assert selection_meta["source"] == "preferred"


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
        adapter, selection_meta = await get_adapter("auto")
    assert isinstance(adapter, CursorAdapter)
    # auto-fallback path: no trigger / env, claude_code unhealthy, cursor wins
    assert selection_meta["source"] == "fallback"


# ---------------------------------------------------------------------------
# v0.31.0 (Phase 5.5): language-weighted platform selection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lang_weight_zero_keeps_claude_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """``AUTODEV_LANG_WEIGHT=0`` (default) preserves the historical Claude bias."""
    monkeypatch.delenv("AUTODEV_PLATFORM", raising=False)
    monkeypatch.setenv("AUTODEV_LANG_WEIGHT", "0.0")
    # Make the cwd look TS-heavy -- if the weight kicked in, Cursor would win.
    (tmp_path / "src").mkdir()
    for i in range(5):
        (tmp_path / "src" / f"app{i}.ts").write_text("export {};\n", encoding="utf-8")

    with (
        patch.object(
            ClaudeCodeAdapter,
            "healthcheck",
            AsyncMock(return_value=(True, "ok")),
        ),
        patch.object(
            CursorAdapter,
            "healthcheck",
            AsyncMock(return_value=(True, "ok")),
        ),
    ):
        name = await detect_platform("auto", cwd=tmp_path)
    assert name == "claude_code"


@pytest.mark.asyncio
async def test_lang_weight_one_picks_by_fitness(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """``AUTODEV_LANG_WEIGHT=1.0`` picks the higher-fitness adapter."""
    monkeypatch.delenv("AUTODEV_PLATFORM", raising=False)
    monkeypatch.setenv("AUTODEV_LANG_WEIGHT", "1.0")
    # TS-heavy cwd -> Cursor wins (95) vs Claude (85).
    (tmp_path / "src").mkdir()
    for i in range(5):
        (tmp_path / "src" / f"app{i}.ts").write_text("export {};\n", encoding="utf-8")

    with (
        patch.object(
            ClaudeCodeAdapter,
            "healthcheck",
            AsyncMock(return_value=(True, "ok")),
        ),
        patch.object(
            CursorAdapter,
            "healthcheck",
            AsyncMock(return_value=(True, "ok")),
        ),
    ):
        name = await detect_platform("auto", cwd=tmp_path)
    assert name == "cursor"


@pytest.mark.asyncio
async def test_explicit_platform_skips_fitness(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """An explicit ``preferred`` platform bypasses the fitness machinery."""
    monkeypatch.delenv("AUTODEV_PLATFORM", raising=False)
    monkeypatch.setenv("AUTODEV_LANG_WEIGHT", "1.0")
    # TS-heavy cwd would pick Cursor under fitness, but the operator
    # asked for claude_code explicitly -- honour that.
    (tmp_path / "src").mkdir()
    for i in range(5):
        (tmp_path / "src" / f"app{i}.ts").write_text("export {};\n", encoding="utf-8")

    with patch.object(
        ClaudeCodeAdapter, "healthcheck", AsyncMock(return_value=(True, "ok"))
    ):
        name = await detect_platform("claude_code", cwd=tmp_path)
    assert name == "claude_code"
