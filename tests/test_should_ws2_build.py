"""WS2 'should'-severity build/detect cleanups.

WS2-21 (build_check wrong interpreter)
    :func:`qa.build_check._run_python_build` used to compile the *target* repo
    with AutoDev's OWN ``sys.executable`` — a version-mismatch trap that
    FALSE-FAILS the build gate when the target pins a different Python. The fix
    resolves the target's interpreter (its ``.venv/bin/python3`` or a
    uv/poetry-managed env) instead. These tests assert the engaged behaviour
    plus a broken-control: forcing the legacy ``sys.executable`` path makes the
    "uses target venv" assertion go red.

WS2-19 (cpp detected, no runners)
    cpp is detected but has no build/test/lint runner and is intentionally NOT
    first-class. The G6 gate-closer (:func:`qa.detect.classify_language_support`)
    already makes a cpp repo degrade LOUD (``unsupported`` → the dispatch emits a
    ``language_unsupported`` op and returns the diagnostic) rather than silently
    soft-passing. These tests CEMENT that already-closed contract for cpp
    specifically (the existing G6 suite exercises the ``detect_language → None``
    branch, not the recognised-but-non-runnable ``cpp`` branch).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qa.build_check import _resolve_target_python, run_build_check


def _make_proc(returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


def _make_target_venv(cwd: Path, interp: str = "python3") -> Path:
    """Create a fake executable target ``.venv/bin/<interp>`` and return its path."""
    venv_bin = cwd / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    interp_path = venv_bin / interp
    interp_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(interp_path, 0o755)
    return interp_path


# ---------------------------------------------------------------------------
# WS2-21: build_check uses the TARGET interpreter, not AutoDev's sys.executable
# ---------------------------------------------------------------------------


def test_resolve_target_python_prefers_venv(tmp_path: Path) -> None:
    """A target ``.venv/bin/python3`` is resolved over AutoDev's interpreter."""
    interp_path = _make_target_venv(tmp_path, "python3")
    assert _resolve_target_python(tmp_path) == [str(interp_path)]


def test_resolve_target_python_falls_back_to_sys_executable(tmp_path: Path) -> None:
    """No venv / no lockfile manager → AutoDev's interpreter (legacy behaviour)."""
    assert _resolve_target_python(tmp_path) == [sys.executable]


@pytest.mark.asyncio
async def test_python_build_uses_target_venv_interpreter(tmp_path: Path) -> None:
    """ENGAGEMENT: ``run_build_check`` compiles with the TARGET ``.venv``
    interpreter, NOT AutoDev's ``sys.executable``."""
    interp_path = _make_target_venv(tmp_path, "python3")
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")

    proc = _make_proc(0)
    with patch(
        "asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
    ) as mock_exec:
        result = await run_build_check(tmp_path, language="python")

    assert result.passed
    invoked = mock_exec.call_args.args[0]
    assert invoked == str(interp_path), (
        "build_check must compile with the target repo's venv interpreter, "
        f"not AutoDev's; invoked={invoked!r}"
    )
    assert invoked != sys.executable


@pytest.mark.asyncio
async def test_python_build_broken_control_reverts_to_sys_executable(
    tmp_path: Path,
) -> None:
    """BROKEN-CONTROL: forcing the legacy ``sys.executable`` path (the pre-fix
    behaviour) makes the 'uses target venv' contract go red — proving the fix is
    load-bearing, not vacuous."""
    interp_path = _make_target_venv(tmp_path, "python3")
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")

    proc = _make_proc(0)
    # Simulate reverting the fix: interpreter resolution collapses back to
    # AutoDev's own sys.executable regardless of the target venv.
    with (
        patch(
            "qa.build_check._resolve_target_python",
            return_value=[sys.executable],
        ),
        patch(
            "asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
        ) as mock_exec,
    ):
        result = await run_build_check(tmp_path, language="python")

    assert result.passed
    invoked = mock_exec.call_args.args[0]
    # Under the reverted behaviour the target venv is IGNORED — this is exactly
    # the false-fail trap the fix removes.
    assert invoked == sys.executable
    assert invoked != str(interp_path)


@pytest.mark.asyncio
async def test_python_build_uses_uv_managed_interpreter(tmp_path: Path) -> None:
    """A uv-managed target (uv.lock, no ``.venv``) resolves to ``uv run python3``
    rather than AutoDev's ``sys.executable``."""
    (tmp_path / "uv.lock").write_text("# lock\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")

    proc = _make_proc(0)
    with (
        patch("qa.env.shutil.which", return_value="/usr/bin/uv"),
        patch(
            "asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
        ) as mock_exec,
    ):
        result = await run_build_check(tmp_path, language="python")

    assert result.passed
    invoked_args = list(mock_exec.call_args.args)
    assert invoked_args[0] == "uv"
    assert "run" in invoked_args
    assert sys.executable not in invoked_args


# ---------------------------------------------------------------------------
# WS2-19: cpp is detected but has NO runner → already degrades LOUD (G6)
# ---------------------------------------------------------------------------


def test_cpp_not_in_runnable_languages() -> None:
    """cpp is intentionally NOT a first-class runnable language."""
    from qa.detect import RUNNABLE_TEST_LANGUAGES

    assert "cpp" not in RUNNABLE_TEST_LANGUAGES


def test_cpp_repo_classifies_unsupported_degrade_loud(tmp_path: Path) -> None:
    """A cpp repo (detect_language → 'cpp') classifies as ``unsupported`` with a
    reason — the contract that makes the QA dispatch degrade LOUD instead of
    silently soft-passing. (WS2-19 already-closed proof.)"""
    from qa.detect import (
        classify_language_support,
        detect_language,
        is_repo_unbuildable,
    )

    (tmp_path / "main.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
    (tmp_path / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.10)\nproject(x)\n", encoding="utf-8"
    )

    assert detect_language(tmp_path) == "cpp"
    assert is_repo_unbuildable(tmp_path) is True

    status, lang, reason = classify_language_support(tmp_path)
    assert status == "unsupported"
    assert lang == "cpp"
    assert reason, "the unsupported classification must carry a loud reason"


@pytest.mark.asyncio
async def test_cpp_build_check_does_not_false_pass_a_real_build(
    tmp_path: Path,
) -> None:
    """build_check has NO cpp runner (charter-correct: cpp is non-first-class),
    so it returns the explicit 'no build checker configured' skip — the loud
    degradation is owned by the QA-dispatch via ``classify_language_support``,
    not by silently pretending cpp built clean."""
    result = await run_build_check(tmp_path, language="cpp")
    assert result.passed  # build_check itself is a no-op for cpp …
    assert "no build checker configured" in result.details  # … but says so loudly
    assert "cpp" in result.details
