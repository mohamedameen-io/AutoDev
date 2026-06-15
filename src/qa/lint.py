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
from qa.env import detect_python_linter, resolve_tool


_DEFAULT_TIMEOUT_S = 60


async def run_lint(
    cwd: Path,
    language: str | None = None,
    *,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    paths: list[Path] | None = None,
) -> GateResult:
    """Run the appropriate linter for *language* (auto-detected when ``None``).

    When *paths* is given (repo-relative changed files), the Python linter is
    scoped to the changed ``.py`` files; otherwise it lints the whole tree.

    Returns a :class:`GateResult` with ``passed=True`` on success or when the
    linter is not available.
    """
    lang = language or detect_language(cwd)
    if lang is None:
        return GateResult(passed=True, details="language not detected, skipping lint")

    if lang == "python":
        return await _run_python_lint(cwd, timeout_s=timeout_s, paths=paths)

    runners: dict[str, object] = {
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


async def _run_python_lint(
    cwd: Path,
    *,
    timeout_s: float,
    paths: list[Path] | None,
) -> GateResult:
    """Run the repo's Python linter (ruff or flake8), scoped to *paths* if given."""
    linter = detect_python_linter(cwd)
    base = resolve_tool(cwd, linter)

    if paths is None:
        # Whole-tree lint (back-compat: a bare repo yields ``["ruff", "check", "."]``).
        args = [*base, "check", "."] if linter == "ruff" else [*base, "."]
        return await _run_subprocess(args, cwd, timeout_s=timeout_s, tool_name=linter)

    # Only lint changed ``.py`` files that actually exist in *cwd*. A changed
    # path absent on disk is a new file not yet materialized in the main
    # worktree (it lands later); passing it to the linter would raise E902
    # (file-not-found) and fail the gate spuriously — the legacy whole-tree
    # ``ruff check .`` simply skipped such paths, so we match that behavior.
    py_files = [str(p) for p in paths if p.suffix == ".py" and (cwd / p).is_file()]
    if not py_files:
        return GateResult(passed=True, details="lint: no changed python files present on disk")
    args = [*base, "check", *py_files] if linter == "ruff" else [*base, *py_files]
    return await _run_subprocess(args, cwd, timeout_s=timeout_s, tool_name=linter)


async def _run_eslint(cwd: Path, *, timeout_s: float) -> GateResult:
    # Prefer local npx eslint; fall back gracefully.
    return await _run_subprocess(["npx", "eslint", "."], cwd, timeout_s=timeout_s, tool_name="eslint")


async def _run_cargo_clippy(cwd: Path, *, timeout_s: float) -> GateResult:
    return await _run_subprocess(["cargo", "clippy"], cwd, timeout_s=timeout_s, tool_name="cargo clippy")


async def _run_golangci_lint(cwd: Path, *, timeout_s: float) -> GateResult:
    return await _run_subprocess(["golangci-lint", "run"], cwd, timeout_s=timeout_s, tool_name="golangci-lint")


__all__ = ["run_lint"]
