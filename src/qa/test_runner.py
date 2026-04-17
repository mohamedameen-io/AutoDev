"""Test-runner gate.

Runs the project's test suite and returns a
:class:`~plugins.registry.GateResult`.

Graceful degradation: if the test runner is not installed, the gate passes
with an informational message.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from plugins.registry import GateResult
from qa.detect import detect_language


_DEFAULT_TIMEOUT_S = 60


async def run_tests(
    cwd: Path,
    language: str | None = None,
    *,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> GateResult:
    """Run the test suite appropriate for *language* (auto-detected when ``None``).

    Returns a :class:`GateResult` with ``passed=True`` on success or when the
    test runner is not available.
    """
    lang = language or detect_language(cwd)
    if lang is None:
        return GateResult(passed=True, details="language not detected, skipping tests")

    runners: dict[str, object] = {
        "python": _run_pytest,
        "nodejs": _run_npm_test,
        "rust": _run_cargo_test,
        "go": _run_go_test,
    }
    runner = runners.get(lang)
    if runner is None:
        return GateResult(passed=True, details=f"no test runner configured for language={lang!r}, skipping")
    return await runner(cwd, timeout_s=timeout_s)  # type: ignore[operator]


async def _run_subprocess(
    args: list[str],
    cwd: Path,
    *,
    timeout_s: float,
    tool_name: str,
) -> GateResult:
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                *args,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ),
            timeout=timeout_s,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except FileNotFoundError:
        return GateResult(passed=True, details=f"{tool_name} not found, skipping tests")
    except asyncio.TimeoutError:
        return GateResult(passed=False, details=f"{tool_name} tests timed out")

    combined = (stdout + stderr).decode(errors="replace").strip()
    if proc.returncode == 0:
        return GateResult(passed=True, details=f"{tool_name} tests passed")
    return GateResult(passed=False, details=f"{tool_name} tests failed:\n{combined}")


async def _run_pytest(cwd: Path, *, timeout_s: float) -> GateResult:
    return await _run_subprocess(["pytest"], cwd, timeout_s=timeout_s, tool_name="pytest")


async def _run_npm_test(cwd: Path, *, timeout_s: float) -> GateResult:
    return await _run_subprocess(["npm", "test"], cwd, timeout_s=timeout_s, tool_name="npm test")


async def _run_cargo_test(cwd: Path, *, timeout_s: float) -> GateResult:
    return await _run_subprocess(["cargo", "test"], cwd, timeout_s=timeout_s, tool_name="cargo test")


async def _run_go_test(cwd: Path, *, timeout_s: float) -> GateResult:
    return await _run_subprocess(["go", "test", "./..."], cwd, timeout_s=timeout_s, tool_name="go test")


__all__ = ["run_tests"]
