"""Lint gate.

Runs the detected linter for the project and returns a
:class:`~plugins.registry.GateResult`.

Graceful degradation: if the linter binary is not installed, the gate passes
with an informational message.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from plugins.registry import GateResult
from qa.detect import detect_language


_DEFAULT_TIMEOUT_S = 60


async def run_lint(
    cwd: Path,
    language: str | None = None,
    *,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> GateResult:
    """Run the appropriate linter for *language* (auto-detected when ``None``).

    Returns a :class:`GateResult` with ``passed=True`` on success or when the
    linter is not available.
    """
    lang = language or detect_language(cwd)
    if lang is None:
        return GateResult(passed=True, details="language not detected, skipping lint")

    runners: dict[str, object] = {
        "python": _run_ruff,
        "nodejs": _run_eslint,
        "rust": _run_cargo_clippy,
        "go": _run_golangci_lint,
    }
    runner = runners.get(lang)
    if runner is None:
        return GateResult(passed=True, details=f"no linter configured for language={lang!r}, skipping")
    return await runner(cwd, timeout_s=timeout_s)  # type: ignore[operator]


async def _run_subprocess(
    args: list[str],
    cwd: Path,
    *,
    timeout_s: float,
    tool_name: str,
) -> GateResult:
    """Generic helper: run *args* in *cwd*, return GateResult."""
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
        return GateResult(passed=True, details=f"{tool_name} not found, skipping lint")
    except asyncio.TimeoutError:
        return GateResult(passed=False, details=f"{tool_name} lint timed out")

    combined = (stdout + stderr).decode(errors="replace").strip()
    if proc.returncode == 0:
        return GateResult(passed=True, details=f"{tool_name} lint passed")
    return GateResult(passed=False, details=f"{tool_name} lint failed:\n{combined}")


async def _run_ruff(cwd: Path, *, timeout_s: float) -> GateResult:
    return await _run_subprocess(["ruff", "check", "."], cwd, timeout_s=timeout_s, tool_name="ruff")


async def _run_eslint(cwd: Path, *, timeout_s: float) -> GateResult:
    # Prefer local npx eslint; fall back gracefully.
    return await _run_subprocess(["npx", "eslint", "."], cwd, timeout_s=timeout_s, tool_name="eslint")


async def _run_cargo_clippy(cwd: Path, *, timeout_s: float) -> GateResult:
    return await _run_subprocess(["cargo", "clippy"], cwd, timeout_s=timeout_s, tool_name="cargo clippy")


async def _run_golangci_lint(cwd: Path, *, timeout_s: float) -> GateResult:
    return await _run_subprocess(["golangci-lint", "run"], cwd, timeout_s=timeout_s, tool_name="golangci-lint")


__all__ = ["run_lint"]
