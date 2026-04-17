"""Syntax-check gate.

Runs a language-appropriate syntax checker against the project and returns a
:class:`~plugins.registry.GateResult`.

Graceful degradation: if the required tool is not found, the gate passes with
an informational message rather than crashing the orchestrator.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from plugins.registry import GateResult
from qa.detect import detect_language


_DEFAULT_TIMEOUT_S = 60


async def run_syntax_check(
    cwd: Path,
    language: str | None = None,
    *,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> GateResult:
    """Run a syntax check appropriate for *language* (auto-detected when ``None``).

    Returns a :class:`GateResult` with ``passed=True`` on success or when the
    required tool is not available (graceful degradation).
    """
    lang = language or detect_language(cwd)
    if lang is None:
        return GateResult(passed=True, details="language not detected, skipping syntax check")

    if lang == "python":
        return await _python_syntax_check(cwd, timeout_s=timeout_s)
    if lang == "nodejs":
        return await _nodejs_syntax_check(cwd, timeout_s=timeout_s)
    # For other languages, skip gracefully — dedicated gates handle them.
    return GateResult(passed=True, details=f"no syntax checker for language={lang!r}, skipping")


async def _python_syntax_check(cwd: Path, *, timeout_s: float) -> GateResult:
    """Compile all .py files under *cwd* using ``python -m py_compile``."""
    py_files = [str(p) for p in cwd.rglob("*.py") if ".venv" not in p.parts and "__pycache__" not in p.parts]
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
        return GateResult(passed=True, details="python not found, skipping syntax check")
    except asyncio.TimeoutError:
        return GateResult(passed=False, details="syntax check timed out")

    if proc.returncode == 0:
        return GateResult(passed=True, details=f"syntax ok ({len(py_files)} files)")
    error_text = stderr.decode(errors="replace").strip() or stdout.decode(errors="replace").strip()
    return GateResult(passed=False, details=f"syntax errors:\n{error_text}")


async def _nodejs_syntax_check(cwd: Path, *, timeout_s: float) -> GateResult:
    """Check JS/TS files using ``node --check``."""
    js_files = [
        str(p)
        for p in cwd.rglob("*.js")
        if "node_modules" not in p.parts and ".venv" not in p.parts
    ]
    if not js_files:
        return GateResult(passed=True, details="no .js files found")

    errors: list[str] = []
    for js_file in js_files:
        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    "node",
                    "--check",
                    js_file,
                    cwd=cwd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                timeout=timeout_s,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except FileNotFoundError:
            return GateResult(passed=True, details="node not found, skipping syntax check")
        except asyncio.TimeoutError:
            return GateResult(passed=False, details="syntax check timed out")

        if proc.returncode != 0:
            errors.append(stderr.decode(errors="replace").strip())

    if errors:
        return GateResult(passed=False, details="syntax errors:\n" + "\n".join(errors))
    return GateResult(passed=True, details=f"syntax ok ({len(js_files)} files)")


__all__ = ["run_syntax_check"]
