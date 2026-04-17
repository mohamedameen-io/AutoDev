"""Tests for :mod:`src.qa.build_check`."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qa.build_check import run_build_check


def _make_proc(returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


@pytest.mark.asyncio
async def test_build_check_no_language(tmp_path: Path) -> None:
    result = await run_build_check(tmp_path)
    assert result.passed
    assert "not detected" in result.details


@pytest.mark.asyncio
async def test_build_check_unknown_language(tmp_path: Path) -> None:
    result = await run_build_check(tmp_path, language="cobol")
    assert result.passed
    assert "skipping" in result.details


@pytest.mark.asyncio
async def test_build_check_python_valid(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("x = 1\n")
    result = await run_build_check(tmp_path, language="python")
    assert result.passed


@pytest.mark.asyncio
async def test_build_check_python_invalid(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text("def broken(\n")
    result = await run_build_check(tmp_path, language="python")
    assert not result.passed


@pytest.mark.asyncio
async def test_build_check_python_no_files(tmp_path: Path) -> None:
    result = await run_build_check(tmp_path, language="python")
    assert result.passed
    assert "no .py files" in result.details


@pytest.mark.asyncio
async def test_build_check_rust_passes(tmp_path: Path) -> None:
    proc = _make_proc(0)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
        result = await run_build_check(tmp_path, language="rust")
    assert result.passed
    assert mock_exec.call_args.args[0] == "cargo"


@pytest.mark.asyncio
async def test_build_check_rust_fails(tmp_path: Path) -> None:
    proc = _make_proc(1, stderr=b"error[E0308]: mismatched types")
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await run_build_check(tmp_path, language="rust")
    assert not result.passed
    assert "E0308" in result.details


@pytest.mark.asyncio
async def test_build_check_go_passes(tmp_path: Path) -> None:
    proc = _make_proc(0)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
        result = await run_build_check(tmp_path, language="go")
    assert result.passed
    assert mock_exec.call_args.args[0] == "go"


@pytest.mark.asyncio
async def test_build_check_tool_not_found(tmp_path: Path) -> None:
    with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
        result = await run_build_check(tmp_path, language="rust")
    assert result.passed
    assert "not found" in result.details


@pytest.mark.asyncio
async def test_build_check_timeout(tmp_path: Path) -> None:
    with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
        result = await run_build_check(tmp_path, language="rust")
    assert not result.passed
    assert "timed out" in result.details


@pytest.mark.asyncio
async def test_build_check_nodejs_with_build_script(tmp_path: Path) -> None:
    import json
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"build": "tsc"}}))
    proc = _make_proc(0)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
        result = await run_build_check(tmp_path, language="nodejs")
    assert result.passed
    assert "npm" in mock_exec.call_args.args[0]


@pytest.mark.asyncio
async def test_build_check_nodejs_no_build_script(tmp_path: Path) -> None:
    import json
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {}}))
    proc = _make_proc(0)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
        result = await run_build_check(tmp_path, language="nodejs")
    assert result.passed
    assert mock_exec.call_args.args[0] == "npx"
