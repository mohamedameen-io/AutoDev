"""Tests for :mod:`src.qa.test_runner`."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qa.test_runner import run_tests


def _make_proc(returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


@pytest.mark.asyncio
async def test_run_tests_no_language(tmp_path: Path) -> None:
    result = await run_tests(tmp_path)
    assert result.passed
    assert "not detected" in result.details


@pytest.mark.asyncio
async def test_run_tests_unknown_language(tmp_path: Path) -> None:
    result = await run_tests(tmp_path, language="cobol")
    assert result.passed
    assert "skipping" in result.details


@pytest.mark.asyncio
async def test_run_tests_python_passes(tmp_path: Path) -> None:
    proc = _make_proc(0, stdout=b"5 passed")
    with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
        result = await run_tests(tmp_path, language="python")
    assert result.passed
    assert mock_exec.call_args.args[0] == "pytest"


@pytest.mark.asyncio
async def test_run_tests_python_fails(tmp_path: Path) -> None:
    proc = _make_proc(1, stdout=b"2 failed, 3 passed")
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        result = await run_tests(tmp_path, language="python")
    assert not result.passed
    assert "failed" in result.details


@pytest.mark.asyncio
async def test_run_tests_nodejs(tmp_path: Path) -> None:
    proc = _make_proc(0)
    with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
        result = await run_tests(tmp_path, language="nodejs")
    assert result.passed
    assert mock_exec.call_args.args[0] == "npm"


@pytest.mark.asyncio
async def test_run_tests_rust(tmp_path: Path) -> None:
    proc = _make_proc(0)
    with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
        result = await run_tests(tmp_path, language="rust")
    assert result.passed
    assert mock_exec.call_args.args[0] == "cargo"


@pytest.mark.asyncio
async def test_run_tests_go(tmp_path: Path) -> None:
    proc = _make_proc(0)
    with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
        result = await run_tests(tmp_path, language="go")
    assert result.passed
    assert mock_exec.call_args.args[0] == "go"


@pytest.mark.asyncio
async def test_run_tests_tool_not_found(tmp_path: Path) -> None:
    with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
        result = await run_tests(tmp_path, language="python")
    assert result.passed
    assert "not found" in result.details


@pytest.mark.asyncio
async def test_run_tests_timeout(tmp_path: Path) -> None:
    with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
        result = await run_tests(tmp_path, language="python")
    assert not result.passed
    assert "timed out" in result.details
