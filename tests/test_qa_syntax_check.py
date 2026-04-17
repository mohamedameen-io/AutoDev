"""Tests for :mod:`src.qa.syntax_check`."""

from __future__ import annotations

from pathlib import Path

import pytest

from qa.syntax_check import run_syntax_check


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
