"""Tests for :mod:`src.qa.lint`."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qa.lint import run_lint


def _make_proc(returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


@pytest.mark.asyncio
async def test_lint_unknown_language(tmp_path: Path) -> None:
    result = await run_lint(tmp_path, language="cobol")
    assert result.passed
    assert "skipping" in result.details


@pytest.mark.asyncio
async def test_lint_no_language_detected(tmp_path: Path) -> None:
    result = await run_lint(tmp_path)
    assert result.passed
    assert "not detected" in result.details


@pytest.mark.asyncio
async def test_lint_python_passes(tmp_path: Path) -> None:
    proc = _make_proc(0)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
        result = await run_lint(tmp_path, language="python")
    assert result.passed
    mock_exec.assert_called_once()
    assert mock_exec.call_args.args[0] == "ruff"


@pytest.mark.asyncio
async def test_lint_python_fails(tmp_path: Path) -> None:
    proc = _make_proc(1, stderr=b"E501 line too long")
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await run_lint(tmp_path, language="python")
    assert not result.passed
    assert "E501" in result.details


@pytest.mark.asyncio
async def test_lint_tool_not_found(tmp_path: Path) -> None:
    with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
        result = await run_lint(tmp_path, language="python")
    assert result.passed
    assert "not found" in result.details


@pytest.mark.asyncio
async def test_lint_timeout(tmp_path: Path) -> None:
    async def _slow(*args, **kwargs):
        raise asyncio.TimeoutError

    with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
        result = await run_lint(tmp_path, language="python")
    assert not result.passed
    assert "timed out" in result.details


@pytest.mark.asyncio
async def test_lint_nodejs(tmp_path: Path) -> None:
    proc = _make_proc(0)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
        result = await run_lint(tmp_path, language="nodejs")
    assert result.passed
    assert mock_exec.call_args.args[0] == "npx"


@pytest.mark.asyncio
async def test_lint_rust(tmp_path: Path) -> None:
    proc = _make_proc(0)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
        result = await run_lint(tmp_path, language="rust")
    assert result.passed
    assert mock_exec.call_args.args[0] == "cargo"


@pytest.mark.asyncio
async def test_lint_go(tmp_path: Path) -> None:
    proc = _make_proc(0)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
        result = await run_lint(tmp_path, language="go")
    assert result.passed
    assert mock_exec.call_args.args[0] == "golangci-lint"
