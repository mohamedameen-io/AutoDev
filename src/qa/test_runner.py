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
from qa.env import resolve_tool


# The orchestrator normally overrides this via ``cfg.test_timeout_s``; the
# default gives real (large) suites headroom rather than the old 60s ceiling
# that could not finish them.
_DEFAULT_TIMEOUT_S = 600


async def run_tests(
    cwd: Path,
    language: str | None = None,
    *,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    paths: list[Path] | None = None,
) -> GateResult:
    """Run the test suite appropriate for *language* (auto-detected when ``None``).

    When *paths* is given (repo-relative changed files), the Python suite is
    scoped to the changed tests (or a bounded default); otherwise the full
    default suite runs.

    Returns a :class:`GateResult` with ``passed=True`` on success or when the
    test runner is not available.
    """
    lang = language or detect_language(cwd)
    if lang is None:
        return GateResult(passed=True, details="language not detected, skipping tests")

    if lang == "python":
        return await _run_pytest(cwd, timeout_s=timeout_s, paths=paths)

    runners: dict[str, object] = {
        "nodejs": _run_npm_test,
        "rust": _run_cargo_test,
        "go": _run_go_test,
    }
    runner = runners.get(lang)
    if runner is None:
        return GateResult(passed=True, details=f"no test runner configured for language={lang!r}, skipping")
    return await runner(cwd, timeout_s=timeout_s)  # type: ignore[operator]


def _is_test_path(p: Path) -> bool:
    """True when *p* looks like a pytest test module or lives under ``tests/``."""
    if p.suffix == ".py" and (p.name.startswith("test_") or p.name.endswith("_test.py")):
        return True
    return "tests" in p.parts


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


async def _run_pytest(
    cwd: Path,
    *,
    timeout_s: float,
    paths: list[Path] | None,
) -> GateResult:
    """Run pytest via the repo's tooling, scoped to changed tests if *paths* given."""
    base = resolve_tool(cwd, "pytest")

    if paths is None:
        targets: list[str] = []
    else:
        changed_tests = [str(p) for p in paths if _is_test_path(p)]
        if changed_tests:
            targets = changed_tests
        elif any(p.suffix == ".py" for p in paths):
            # Source-only change: run a bounded default suite when present.
            targets = ["tests/unit"] if (cwd / "tests" / "unit").is_dir() else []
        else:
            return GateResult(passed=True, details="tests: no python changes")

    args = [*base, *targets, "-q"]
    return await _run_subprocess(args, cwd, timeout_s=timeout_s, tool_name="pytest")


async def _run_npm_test(cwd: Path, *, timeout_s: float) -> GateResult:
    return await _run_subprocess(["npm", "test"], cwd, timeout_s=timeout_s, tool_name="npm test")


async def _run_cargo_test(cwd: Path, *, timeout_s: float) -> GateResult:
    return await _run_subprocess(["cargo", "test"], cwd, timeout_s=timeout_s, tool_name="cargo test")


async def _run_go_test(cwd: Path, *, timeout_s: float) -> GateResult:
    return await _run_subprocess(["go", "test", "./..."], cwd, timeout_s=timeout_s, tool_name="go test")


__all__ = ["run_tests"]
