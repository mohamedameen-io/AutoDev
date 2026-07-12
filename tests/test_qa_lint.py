"""Tests for :mod:`src.qa.lint`."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qa.lint import run_lint


def _make_proc(returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


def _make_target_venv_python(cwd: Path, name: str = "python3") -> Path:
    """Create an executable target ``.venv/bin/<name>`` interpreter and return it."""
    interp = cwd / ".venv" / "bin" / name
    interp.parent.mkdir(parents=True, exist_ok=True)
    interp.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(interp, 0o755)
    return interp


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
    # stabilization-v1: _run_eslint now pre-checks for an eslint config and
    # skips when none is found (no-config → passed=True without calling the
    # subprocess).  Create a config so the subprocess path is exercised.
    (tmp_path / "eslint.config.js").write_text("export default [];\n", encoding="utf-8")
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


# ---------------------------------------------------------------------------
# WS-6b: flake8 must run under the TARGET repo interpreter, not AutoDev's host
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lint_flake8_uses_target_venv_interpreter(tmp_path: Path) -> None:
    """ENGAGEMENT: with a ``.flake8`` config and a target ``.venv`` python present
    (but no ``.venv/bin/flake8``), the gate runs flake8 UNDER the target
    interpreter (``python -m flake8``) — not the bare host flake8 that crashes
    under AutoDev's py3.13 (the django-10914 / pylint-5859 field failure)."""
    interp = _make_target_venv_python(tmp_path, "python3")
    (tmp_path / ".flake8").write_text("[flake8]\n", encoding="utf-8")

    proc = _make_proc(0)
    with patch(
        "asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
    ) as mock_exec:
        result = await run_lint(tmp_path, language="python")

    assert result.passed
    args = list(mock_exec.call_args.args)
    assert args[0] == str(interp), (
        "flake8 must run under the target repo's venv interpreter, "
        f"not AutoDev's host; invoked={args[0]!r}"
    )
    assert args[0] != sys.executable
    assert "-m" in args and "flake8" in args


@pytest.mark.asyncio
async def test_lint_flake8_no_venv_falls_back_to_bare_host(tmp_path: Path) -> None:
    """FALLBACK: no target venv → the bare host flake8 (unchanged legacy behaviour)."""
    (tmp_path / ".flake8").write_text("[flake8]\n", encoding="utf-8")

    proc = _make_proc(0)
    with patch(
        "asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
    ) as mock_exec:
        result = await run_lint(tmp_path, language="python")

    assert result.passed
    assert mock_exec.call_args.args[0] == "flake8"


@pytest.mark.asyncio
async def test_lint_ruff_stays_host_binary_even_with_venv_python(tmp_path: Path) -> None:
    """CONTROL: ruff is a self-contained (version-agnostic) binary, so it is NOT
    routed through the target interpreter. Even with a target ``.venv`` python
    present, ruff resolves to the bare host binary (no ``python -m ruff``), which
    avoids a false-fail when ruff is only installed on the host."""
    _make_target_venv_python(tmp_path, "python3")
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")

    proc = _make_proc(0)
    with patch(
        "asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
    ) as mock_exec:
        result = await run_lint(tmp_path, language="python")

    assert result.passed
    args = list(mock_exec.call_args.args)
    assert args[0] == "ruff"
    assert "-m" not in args


@pytest.mark.asyncio
async def test_lint_future_pure_python_linter_routes_through_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DENY-LIST DEFAULT: a linter NOT in ``_SELF_CONTAINED_LINTERS`` (here a
    hypothetical future ``pylint`` value from ``detect_python_linter``) is treated
    as a version-sensitive pure-Python tool and routed through the TARGET
    interpreter (``python -m pylint``), NOT the bare host. Pins the deny-list
    intent so adding a future pure-Python linter is correct-by-construction and
    cannot silently reintroduce the host-interpreter crash."""
    interp = _make_target_venv_python(tmp_path, "python3")
    monkeypatch.setattr("qa.lint.detect_python_linter", lambda _cwd: "pylint")

    proc = _make_proc(0)
    with patch(
        "asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
    ) as mock_exec:
        result = await run_lint(tmp_path, language="python")

    assert result.passed
    args = list(mock_exec.call_args.args)
    assert args[0] == str(interp)
    assert args[0] != sys.executable
    assert "-m" in args and "pylint" in args
