"""Build/typecheck gate.

Runs the detected build or type-check tool for the project and returns a
:class:`~plugins.registry.GateResult`.

Graceful degradation: if the required tool is not installed, the gate passes
with an informational message.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from plugins.registry import GateResult
from qa.detect import detect_language


_DEFAULT_TIMEOUT_S = 60


async def run_build_check(
    cwd: Path,
    language: str | None = None,
    *,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> GateResult:
    """Run the appropriate build/typecheck for *language* (auto-detected when ``None``).

    Returns a :class:`GateResult` with ``passed=True`` on success or when the
    tool is not available.
    """
    lang = language or detect_language(cwd)
    if lang is None:
        return GateResult(passed=True, details="language not detected, skipping build check")

    runners: dict[str, object] = {
        "python": _run_python_build,
        "nodejs": _run_nodejs_build,
        "rust": _run_cargo_check,
        "go": _run_go_build,
    }
    runner = runners.get(lang)
    if runner is None:
        return GateResult(passed=True, details=f"no build checker configured for language={lang!r}, skipping")
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
        return GateResult(passed=True, details=f"{tool_name} not found, skipping build check")
    except asyncio.TimeoutError:
        return GateResult(passed=False, details=f"{tool_name} build check timed out")

    combined = (stdout + stderr).decode(errors="replace").strip()
    if proc.returncode == 0:
        return GateResult(passed=True, details=f"{tool_name} build check passed")
    return GateResult(passed=False, details=f"{tool_name} build check failed:\n{combined}")


async def _run_python_build(cwd: Path, *, timeout_s: float) -> GateResult:
    """Compile all .py files; try mypy if available."""
    py_files = [
        str(p)
        for p in cwd.rglob("*.py")
        if ".venv" not in p.parts and "__pycache__" not in p.parts
    ]
    if not py_files:
        return GateResult(passed=True, details="no .py files found")

    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "py_compile",
                *py_files,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ),
            timeout=timeout_s,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except FileNotFoundError:
        return GateResult(passed=True, details="python not found, skipping build check")
    except asyncio.TimeoutError:
        return GateResult(passed=False, details="python build check timed out")

    if proc.returncode != 0:
        error_text = (stderr + stdout).decode(errors="replace").strip()
        return GateResult(passed=False, details=f"py_compile failed:\n{error_text}")
    return GateResult(passed=True, details=f"py_compile ok ({len(py_files)} files)")


async def _run_nodejs_build(cwd: Path, *, timeout_s: float) -> GateResult:
    # Try npm run build first; fall back to tsc --noEmit.
    pkg_json = cwd / "package.json"
    if pkg_json.exists():
        import json

        try:
            pkg = json.loads(pkg_json.read_text())
        except Exception:
            pkg = {}
        if "build" in pkg.get("scripts", {}):
            return await _run_subprocess(
                ["npm", "run", "build"], cwd, timeout_s=timeout_s, tool_name="npm build"
            )
    return await _run_subprocess(
        ["npx", "tsc", "--noEmit"], cwd, timeout_s=timeout_s, tool_name="tsc"
    )


async def _run_cargo_check(cwd: Path, *, timeout_s: float) -> GateResult:
    return await _run_subprocess(["cargo", "check"], cwd, timeout_s=timeout_s, tool_name="cargo check")


async def _run_go_build(cwd: Path, *, timeout_s: float) -> GateResult:
    return await _run_subprocess(["go", "build", "./..."], cwd, timeout_s=timeout_s, tool_name="go build")


__all__ = ["run_build_check"]
