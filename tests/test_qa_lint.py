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
    # WS2-6 golden-baseline shift: a missing linter used to silently pass
    # (passed=True, "not found, skipping"). A missing toolchain is *unknown*,
    # not *clean* — the gate now degrades LOUD with a toolchain-missing signal.
    with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
        result = await run_lint(tmp_path, language="python")
    assert not result.passed
    assert "not installed" in result.details
    assert result.metrics.get("skipped_toolchain_missing") is True


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


@pytest.mark.asyncio
async def test_lint_python_flake8_command(tmp_path: Path) -> None:
    (tmp_path / ".flake8").write_text("[flake8]\n", encoding="utf-8")
    proc = _make_proc(0)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
        result = await run_lint(tmp_path, language="python")
    assert result.passed
    args = list(mock_exec.call_args.args)
    assert args[0] == "flake8"
    assert "check" not in args  # flake8 has no `check` subcommand


@pytest.mark.asyncio
async def test_lint_python_ruff_command(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
    proc = _make_proc(0)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
        result = await run_lint(tmp_path, language="python")
    assert result.passed
    args = list(mock_exec.call_args.args)
    assert args[0] == "ruff"
    assert "check" in args


@pytest.mark.asyncio
async def test_lint_python_paths_scoped(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    proc = _make_proc(0)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
        result = await run_lint(
            tmp_path, language="python", paths=[Path("a.py"), Path("b.txt")]
        )
    assert result.passed
    args = list(mock_exec.call_args.args)
    assert "a.py" in args
    assert "b.txt" not in args
    assert "." not in args


@pytest.mark.asyncio
async def test_lint_python_paths_no_python(tmp_path: Path) -> None:
    with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
        result = await run_lint(tmp_path, language="python", paths=[Path("README.md")])
    assert result.passed
    assert "no changed python files" in result.details
    mock_exec.assert_not_called()


@pytest.mark.asyncio
async def test_lint_python_paths_skips_absent_new_file(tmp_path: Path) -> None:
    # A changed .py path not yet materialized in cwd (a new file that lands
    # later) must be skipped, not passed to the linter — passing it would raise
    # E902 (file-not-found) and fail the gate spuriously. Regression guard.
    with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
        result = await run_lint(tmp_path, language="python", paths=[Path("new_module.py")])
    assert result.passed
    assert "present on disk" in result.details
    mock_exec.assert_not_called()
