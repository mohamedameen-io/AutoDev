"""v0.37.0 H4 integration smoke: trigger-context wins in ``preferred="auto"``.

Verifies the end-to-end precedence rule that retros must be able to
rely on: when AutoDev is launched from inside a Claude Code session,
``detect_platform`` returns ``claude_code`` even if ``AUTODEV_PLATFORM``
is set to ``cursor``. The ``--platform cursor`` (explicit preferred)
escape hatch still wins.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from adapters.claude_code import ClaudeCodeAdapter
from adapters.cursor import CursorAdapter
from adapters.detect import detect_platform


@pytest.fixture(autouse=True)
def _clear_trigger_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    monkeypatch.delenv("AUTODEV_PLATFORM", raising=False)
    monkeypatch.delenv("AUTODEV_LANG_WEIGHT", raising=False)
    for key in [k for k in list(os.environ) if k.startswith("CURSOR_")]:
        monkeypatch.delenv(key, raising=False)


@pytest.mark.asyncio
async def test_claudecode_env_routes_to_claude_in_auto(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    with patch.object(
        ClaudeCodeAdapter, "healthcheck", AsyncMock(return_value=(True, "ok"))
    ):
        name = await detect_platform("auto", cwd=tmp_path)
    assert name == "claude_code"


@pytest.mark.asyncio
async def test_claudecode_env_beats_autodev_platform_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The headline behaviour change in v0.37.0 H4."""
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("AUTODEV_PLATFORM", "cursor")
    with patch.object(
        ClaudeCodeAdapter, "healthcheck", AsyncMock(return_value=(True, "ok"))
    ):
        name = await detect_platform("auto", cwd=tmp_path)
    assert name == "claude_code"


@pytest.mark.asyncio
async def test_explicit_cursor_still_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--platform cursor`` overrides the host context."""
    monkeypatch.setenv("CLAUDECODE", "1")
    with patch.object(
        CursorAdapter, "healthcheck", AsyncMock(return_value=(True, "ok"))
    ):
        name = await detect_platform("cursor", cwd=tmp_path)
    assert name == "cursor"
