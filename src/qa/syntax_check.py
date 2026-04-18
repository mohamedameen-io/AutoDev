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


_PY_COMPILE_BATCH_SIZE = 128  # max files per subprocess invocation (avoids ARG_MAX)
_PY_COMPILE_BATCH_BYTES = 128 * 1024  # ~128 KB of total path length per batch


def _batch_files(files: list[str], max_count: int, max_bytes: int) -> list[list[str]]:
    """Split *files* into batches capped by count and cumulative path-length bytes."""
    batches: list[list[str]] = []
    current: list[str] = []
    current_bytes = 0
    for f in files:
        f_len = len(f.encode()) + 1  # +1 for the NUL/space separator
        if current and (len(current) >= max_count or current_bytes + f_len > max_bytes):
            batches.append(current)
            current = []
            current_bytes = 0
        current.append(f)
        current_bytes += f_len
    if current:
        batches.append(current)
    return batches


async def _python_syntax_check(cwd: Path, *, timeout_s: float) -> GateResult:
    """Compile all .py files under *cwd* using ``python -m py_compile``.

    Files are split into batches of at most :data:`_PY_COMPILE_BATCH_SIZE`
    entries (or :data:`_PY_COMPILE_BATCH_BYTES` of total path length) to stay
    within the OS ``ARG_MAX`` limit on large projects.
    """
    py_files = [str(p) for p in cwd.rglob("*.py") if ".venv" not in p.parts and "__pycache__" not in p.parts]
    if not py_files:
        return GateResult(passed=True, details="no .py files found")

    batches = _batch_files(py_files, _PY_COMPILE_BATCH_SIZE, _PY_COMPILE_BATCH_BYTES)
    all_errors: list[str] = []

    for batch in batches:
        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "py_compile",
                    *batch,
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

        if proc.returncode != 0:
            error_text = stderr.decode(errors="replace").strip() or stdout.decode(errors="replace").strip()
            all_errors.append(error_text)

    if all_errors:
        return GateResult(passed=False, details="syntax errors:\n" + "\n".join(all_errors))
    return GateResult(passed=True, details=f"syntax ok ({len(py_files)} files)")


async def _nodejs_syntax_check(cwd: Path, *, timeout_s: float) -> GateResult:
    """Check JS files using ``node --check``.

    Note: ``node --check`` only parses plain JavaScript; TypeScript files are
    intentionally excluded because Node cannot parse TS syntax directly.
    """
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
