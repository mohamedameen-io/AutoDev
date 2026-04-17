"""Tests for :mod:`src.qa.syntax_check`."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qa.syntax_check import run_syntax_check


# ---------------------------------------------------------------------------
# Existing integration-style tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_syntax_check_valid_python(tmp_path: Path) -> None:
    (tmp_path / "good.py").write_text("def add(a, b):\n    return a + b\n")
    result = await run_syntax_check(tmp_path, language="python")
    assert result.passed


@pytest.mark.asyncio
async def test_syntax_check_invalid_python(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text("def broken(\n")
    result = await run_syntax_check(tmp_path, language="python")
    assert not result.passed
    assert result.details


@pytest.mark.asyncio
async def test_syntax_check_no_py_files(tmp_path: Path) -> None:
    result = await run_syntax_check(tmp_path, language="python")
    assert result.passed
    assert "no .py files" in result.details


@pytest.mark.asyncio
async def test_syntax_check_unknown_language(tmp_path: Path) -> None:
    result = await run_syntax_check(tmp_path, language="cobol")
    assert result.passed
    assert "skipping" in result.details


@pytest.mark.asyncio
async def test_syntax_check_auto_detect_python(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "app.py").write_text("x = 1\n")
    result = await run_syntax_check(tmp_path)
    assert result.passed


@pytest.mark.asyncio
async def test_syntax_check_no_language_detected(tmp_path: Path) -> None:
    result = await run_syntax_check(tmp_path)
    assert result.passed
    assert "not detected" in result.details


@pytest.mark.asyncio
async def test_syntax_check_skips_venv(tmp_path: Path) -> None:
    """Files inside .venv should not be checked."""
    venv_dir = tmp_path / ".venv" / "lib"
    venv_dir.mkdir(parents=True)
    (venv_dir / "broken.py").write_text("def broken(\n")
    (tmp_path / "good.py").write_text("x = 1\n")
    result = await run_syntax_check(tmp_path, language="python")
    assert result.passed


# ---------------------------------------------------------------------------
# Python syntax-check: mocked subprocess tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_syntax_check_python_passes(tmp_path: Path) -> None:
    """Mocked subprocess returns 0 -> passed."""
    (tmp_path / "app.py").write_text("x = 1\n")
    proc = MagicMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(b"", b""))
    with patch("asyncio.wait_for", new=AsyncMock(side_effect=[proc, (b"", b"")])):
        result = await run_syntax_check(tmp_path, language="python")
    assert result.passed
    assert "syntax ok" in result.details


@pytest.mark.asyncio
async def test_syntax_check_python_fails(tmp_path: Path) -> None:
    """Mocked subprocess returns non-zero with stderr -> failed."""
    (tmp_path / "bad.py").write_text("x\n")
    proc = MagicMock()
    proc.returncode = 1
    proc.communicate = AsyncMock(return_value=(b"", b"SyntaxError: invalid syntax"))
    with patch("asyncio.wait_for", new=AsyncMock(side_effect=[proc, (b"", b"SyntaxError: invalid syntax")])):
        result = await run_syntax_check(tmp_path, language="python")
    assert not result.passed
    assert "syntax errors" in result.details
    assert "SyntaxError" in result.details


@pytest.mark.asyncio
async def test_syntax_check_python_no_files(tmp_path: Path) -> None:
    """Empty directory, no .py files -> passed with informational message."""
    result = await run_syntax_check(tmp_path, language="python")
    assert result.passed
    assert "no .py files" in result.details


@pytest.mark.asyncio
async def test_syntax_check_python_not_found(tmp_path: Path) -> None:
    """FileNotFoundError when python is not available -> graceful pass."""
    (tmp_path / "hello.py").write_text("print('hi')\n")
    with patch("asyncio.wait_for", new=AsyncMock(side_effect=FileNotFoundError)):
        result = await run_syntax_check(tmp_path, language="python")
    assert result.passed
    assert "python not found" in result.details


@pytest.mark.asyncio
async def test_syntax_check_python_timeout(tmp_path: Path) -> None:
    """asyncio.TimeoutError -> failed with timeout message."""
    (tmp_path / "slow.py").write_text("x = 1\n")
    with patch("asyncio.wait_for", new=AsyncMock(side_effect=asyncio.TimeoutError)):
        result = await run_syntax_check(tmp_path, language="python")
    assert not result.passed
    assert "timed out" in result.details


# ---------------------------------------------------------------------------
# Node.js syntax-check: mocked subprocess tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_syntax_check_nodejs_passes(tmp_path: Path) -> None:
    """node --check passes for all .js files -> passed."""
    (tmp_path / "index.js").write_text("const x = 1;\n")
    (tmp_path / "util.js").write_text("module.exports = {};\n")
    proc = MagicMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(b"", b""))
    # Each .js file triggers two wait_for calls: one for create_subprocess_exec, one for communicate
    with patch("asyncio.wait_for", new=AsyncMock(side_effect=[proc, (b"", b""), proc, (b"", b"")])):
        result = await run_syntax_check(tmp_path, language="nodejs")
    assert result.passed
    assert "syntax ok" in result.details
    assert "2 files" in result.details


@pytest.mark.asyncio
async def test_syntax_check_nodejs_fails(tmp_path: Path) -> None:
    """node --check fails for a .js file -> failed with error details."""
    (tmp_path / "bad.js").write_text("function broken(\n")
    proc = MagicMock()
    proc.returncode = 1
    proc.communicate = AsyncMock(return_value=(b"", b"SyntaxError: Unexpected end of input"))
    with patch("asyncio.wait_for", new=AsyncMock(side_effect=[proc, (b"", b"SyntaxError: Unexpected end of input")])):
        result = await run_syntax_check(tmp_path, language="nodejs")
    assert not result.passed
    assert "syntax errors" in result.details
    assert "SyntaxError" in result.details


@pytest.mark.asyncio
async def test_syntax_check_nodejs_no_files(tmp_path: Path) -> None:
    """No .js files in directory -> passed with informational message."""
    result = await run_syntax_check(tmp_path, language="nodejs")
    assert result.passed
    assert "no .js files" in result.details


@pytest.mark.asyncio
async def test_syntax_check_nodejs_not_found(tmp_path: Path) -> None:
    """FileNotFoundError when node is not available -> graceful pass."""
    (tmp_path / "app.js").write_text("const x = 1;\n")
    with patch("asyncio.wait_for", new=AsyncMock(side_effect=FileNotFoundError)):
        result = await run_syntax_check(tmp_path, language="nodejs")
    assert result.passed
    assert "node not found" in result.details


@pytest.mark.asyncio
async def test_syntax_check_nodejs_timeout(tmp_path: Path) -> None:
    """asyncio.TimeoutError during node --check -> failed with timeout message."""
    (tmp_path / "slow.js").write_text("const x = 1;\n")
    with patch("asyncio.wait_for", new=AsyncMock(side_effect=asyncio.TimeoutError)):
        result = await run_syntax_check(tmp_path, language="nodejs")
    assert not result.passed
    assert "timed out" in result.details
