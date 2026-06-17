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
    # WS2-3 golden-baseline shift (feature-now-active): an unsupported /
    # SAFE-DEGRADE language used to silent-pass (passed=True, "skipping"). Per
    # the cross-cutting degrade-loud convention it now degrades LOUD —
    # passed=False + an ``unsupported_language`` marker — so the resolver
    # treats "we can't run this toolchain" as blocking, not a clean green.
    result = await run_tests(tmp_path, language="cobol")
    assert not result.passed
    assert result.metrics.get("unsupported_language") is True


@pytest.mark.asyncio
async def test_run_tests_python_passes(tmp_path: Path) -> None:
    proc = _make_proc(0, stdout=b"5 passed")
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
        result = await run_tests(tmp_path, language="python")
    assert result.passed
    assert mock_exec.call_args.args[0] == "pytest"


@pytest.mark.asyncio
async def test_run_tests_python_fails(tmp_path: Path) -> None:
    proc = _make_proc(1, stdout=b"2 failed, 3 passed")
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await run_tests(tmp_path, language="python")
    assert not result.passed
    assert "failed" in result.details


@pytest.mark.asyncio
async def test_run_tests_nodejs(tmp_path: Path) -> None:
    proc = _make_proc(0)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
        result = await run_tests(tmp_path, language="nodejs")
    assert result.passed
    assert mock_exec.call_args.args[0] == "npm"


@pytest.mark.asyncio
async def test_run_tests_rust(tmp_path: Path) -> None:
    proc = _make_proc(0)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
        result = await run_tests(tmp_path, language="rust")
    assert result.passed
    assert mock_exec.call_args.args[0] == "cargo"


@pytest.mark.asyncio
async def test_run_tests_go(tmp_path: Path) -> None:
    proc = _make_proc(0)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
        result = await run_tests(tmp_path, language="go")
    assert result.passed
    assert mock_exec.call_args.args[0] == "go"


@pytest.mark.asyncio
async def test_run_tests_tool_not_found(tmp_path: Path) -> None:
    # WS2-6 golden-baseline shift: an absent test runner used to silently pass
    # (passed=True, "not found, skipping tests"). A missing toolchain is
    # *unknown*, not *clean* — the gate now degrades LOUD so the resolver
    # treats it as blocking rather than a vacuous green.
    with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
        result = await run_tests(tmp_path, language="python")
    assert not result.passed
    assert "not installed" in result.details
    assert result.metrics.get("skipped_toolchain_missing") is True


@pytest.mark.asyncio
async def test_run_tests_timeout(tmp_path: Path) -> None:
    with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
        result = await run_tests(tmp_path, language="python")
    assert not result.passed
    assert "timed out" in result.details


@pytest.mark.asyncio
async def test_run_tests_default_suite_args(tmp_path: Path) -> None:
    # Back-compat: paths=None → bare suite with just ``-q`` appended.
    proc = _make_proc(0, stdout=b"5 passed")
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
        result = await run_tests(tmp_path, language="python")
    assert result.passed
    assert list(mock_exec.call_args.args) == ["pytest", "-q"]


@pytest.mark.asyncio
async def test_run_tests_paths_changed_test(tmp_path: Path) -> None:
    proc = _make_proc(0, stdout=b"1 passed")
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
        result = await run_tests(
            tmp_path, language="python", paths=[Path("tests/test_foo.py")]
        )
    assert result.passed
    args = list(mock_exec.call_args.args)
    assert args[0] == "pytest"
    assert "tests/test_foo.py" in args
    assert "-q" in args


@pytest.mark.asyncio
async def test_run_tests_paths_source_with_tests_unit(tmp_path: Path) -> None:
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    proc = _make_proc(0, stdout=b"5 passed")
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
        result = await run_tests(tmp_path, language="python", paths=[Path("src/foo.py")])
    assert result.passed
    args = list(mock_exec.call_args.args)
    assert "tests/unit" in args


@pytest.mark.asyncio
async def test_run_tests_paths_source_without_tests_unit(tmp_path: Path) -> None:
    proc = _make_proc(0, stdout=b"5 passed")
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
        result = await run_tests(tmp_path, language="python", paths=[Path("src/foo.py")])
    assert result.passed
    assert list(mock_exec.call_args.args) == ["pytest", "-q"]


@pytest.mark.asyncio
async def test_run_tests_paths_no_python(tmp_path: Path) -> None:
    with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
        result = await run_tests(tmp_path, language="python", paths=[Path("docs/readme.md")])
    assert result.passed
    assert "no python changes" in result.details
    mock_exec.assert_not_called()
