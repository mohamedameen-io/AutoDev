"""Live smoke tests — opt-in via AUTODEV_LIVE=1.

These actually spawn real `claude -p` / `cursor agent` subprocesses. They are
skipped by default because each call consumes real subscription quota.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from adapters.claude_code import ClaudeCodeAdapter
from adapters.cursor import CursorAdapter
from adapters.types import AgentInvocation


_LIVE = os.environ.get("AUTODEV_LIVE") == "1"


def _init_git(tmp_path: Path) -> Path:
    """Initialize an empty git repo in tmp_path."""
    subprocess.run(
        ["git", "init", "-q"],
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / "README.md").write_text("initial\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "."],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=test",
            "commit",
            "-qm",
            "init",
        ],
        cwd=tmp_path,
        check=True,
    )
    return tmp_path


@pytest.mark.skipif(not _LIVE, reason="AUTODEV_LIVE=1 not set")
@pytest.mark.asyncio
async def test_claude_live_ping(tmp_path: Path) -> None:
    adapter = ClaudeCodeAdapter()
    ok, _ = await adapter.healthcheck()
    if not ok:
        pytest.skip("claude CLI not available / not logged in")
    _init_git(tmp_path)
    inv = AgentInvocation(
        role="echo",
        prompt="Reply with exactly: PING_OK",
        cwd=tmp_path,
        model="haiku",
        max_turns=1,
        timeout_s=60,
    )
    result = await adapter.execute(inv)
    assert result.success, result.error
    assert "PING_OK" in result.text


@pytest.mark.skipif(not _LIVE, reason="AUTODEV_LIVE=1 not set")
@pytest.mark.asyncio
async def test_cursor_live_ping(tmp_path: Path) -> None:
    adapter = CursorAdapter()
    ok, _ = await adapter.healthcheck()
    if not ok:
        pytest.skip("cursor CLI not available / not logged in")
    _init_git(tmp_path)
    inv = AgentInvocation(
        role="echo",
        prompt="Reply with exactly: PING_OK",
        cwd=tmp_path,
        max_turns=1,
        timeout_s=60,
    )
    result = await adapter.execute(inv)
    assert result.success, result.error
    assert "PING_OK" in result.text
