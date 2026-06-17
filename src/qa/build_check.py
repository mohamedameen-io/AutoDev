"""Build/typecheck gate.

Runs the detected build or type-check tool for the project and returns a
:class:`~plugins.registry.GateResult`.

Toolchain-absent degrade-LOUD (WS2-6)
-------------------------------------
A missing build toolchain is *unknown*, not *clean*. When the build binary is
absent (``FileNotFoundError``) this gate now returns ``passed=False`` with a
``skipped_toolchain_missing`` signal instead of silently passing — so the
resolver treats it as blocking rather than a vacuous green.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from plugins.registry import GateResult
from qa._scan_filter import collect_scan_files
from qa.detect import detect_language
from qa.env import resolve_tool


# WS2-11: the orchestrator normally overrides this via
# ``cfg.qa_gates.build_check_timeout_s`` (threaded into ``run_build_check``).
# The default floor is 120s — the legacy 60s could not finish a COLD cargo/Go
# build (dependency fetch + first compile) and false-blocked on timeout.
_DEFAULT_TIMEOUT_S = 120


async def run_build_check(
    cwd: Path,
    language: str | None = None,
    *,
    paths: list[Path] | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> GateResult:
    """Run the appropriate build/typecheck for *language* (auto-detected when ``None``).

    Returns a :class:`GateResult` with ``passed=True`` on success or when the
    tool is not available.

    G7 (submodule + generated exclude): the Python compile step no longer
    descends into submodule trees (parsed from ``.gitmodules``), ``.git`` /
    ``.git/modules`` internals, vendored/cache dirs, or generated stubs
    (``*_pb2.py`` …) — a syntax error in any of those must not block an
    unrelated fix.

    S2 / WS2-16 (diff-scoping): when *paths* (repo-relative changed files) is
    given, the Python compile step is scoped to those files instead of the
    whole tree (O(diff) instead of O(repo)). Generated files are skipped even
    when explicitly listed. ``paths=None`` preserves whole-tree behavior. The
    subprocess-based builds (rust/go/npm/tsc) are project-wide by nature and
    ignore *paths*; the parameter is accepted uniformly for caller back-compat.
    """
    lang = language or detect_language(cwd)
    if lang is None:
        return GateResult(passed=True, details="language not detected, skipping build check")

    if lang == "python":
        return await _run_python_build(cwd, paths=paths, timeout_s=timeout_s)

    runners: dict[str, object] = {
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
        # WS2-6: degrade loud — a missing build toolchain is unknown, not clean.
        return GateResult(
            passed=False,
            details=(
                f"{tool_name} not installed: build toolchain missing "
                "(skipped_toolchain_missing)"
            ),
            metrics={"skipped_toolchain_missing": True, "tool": tool_name},
        )
    except asyncio.TimeoutError:
        return GateResult(passed=False, details=f"{tool_name} build check timed out")

    combined = (stdout + stderr).decode(errors="replace").strip()
    if proc.returncode == 0:
        return GateResult(passed=True, details=f"{tool_name} build check passed")
    return GateResult(passed=False, details=f"{tool_name} build check failed:\n{combined}")


def _resolve_target_python(cwd: Path) -> list[str]:
    """Return the argv prefix of the *target* repo's Python interpreter.

    WS2-21: compiling the target repo with AutoDev's OWN ``sys.executable`` is a
    version-mismatch trap — AutoDev runs on (say) 3.13 while the target pins
    3.9, so 3.9-valid syntax (or 3.13-removed stdlib) makes ``py_compile``
    FALSE-FAIL the build gate on otherwise-clean code. Resolve the *target's*
    interpreter instead.

    Cascade (first match wins):

    * ``<cwd>/.venv/bin/python3`` / ``<cwd>/.venv/bin/python`` — the repo's own
      virtualenv interpreter (direct, no PATH guesswork).
    * :func:`qa.env.resolve_tool` for ``python3`` then ``python`` — this also
      surfaces uv/poetry-managed interpreters (``uv run python3 -m …``) and any
      ``.venv/bin/python*`` it finds. ``resolve_tool`` returns the *bare*
      ``[tool]`` as its last-resort fallback; that is NOT a target-specific
      resolution (it would shell out to whatever ``python``/``python3`` is on
      PATH, defeating the point), so we ignore the bare form and keep cascading.
    * ``sys.executable`` — only when nothing target-specific resolves (a repo
      with no venv / no lockfile manager): AutoDev's interpreter is the best
      available, matching the legacy behaviour for that case.
    """
    for direct in (".venv/bin/python3", ".venv/bin/python"):
        candidate = cwd / direct
        if candidate.exists():
            return [str(candidate)]
    for tool in ("python3", "python"):
        argv = resolve_tool(cwd, tool)
        # Skip the bare ``[tool]`` last-resort — only accept a managed/venv form.
        if argv != [tool]:
            return argv
    return [sys.executable]


async def _run_python_build(
    cwd: Path, *, paths: list[Path] | None = None, timeout_s: float
) -> GateResult:
    """Compile all .py files (or the diff-scoped subset when *paths* is given).

    G7: submodule trees, ``.git``/``.git/modules``, vendored/cache dirs, and
    generated stubs (``*_pb2.py`` …) are excluded.
    S2: when *paths* is non-``None`` only those files are compiled.
    WS2-21: compiled with the *target* repo's interpreter (see
    :func:`_resolve_target_python`), not AutoDev's ``sys.executable``.
    """
    py_files = [str(p) for p in collect_scan_files(cwd, (".py",), paths=paths)]
    if not py_files:
        return GateResult(passed=True, details="no .py files found")

    interpreter = _resolve_target_python(cwd)
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                *interpreter,
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
        # WS2-6: the Python interpreter / py_compile is unavailable → degrade
        # loud rather than silently green.
        return GateResult(
            passed=False,
            details=(
                "python not installed: build toolchain missing "
                "(skipped_toolchain_missing)"
            ),
            metrics={"skipped_toolchain_missing": True, "tool": "python"},
        )
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

    # WS2-9: only run ``tsc`` when a tsconfig.json exists. Config-less ``tsc``
    # compiles every .js it can find and fails on perfectly valid JS-only repos
    # (e.g. ``error TS18003: No inputs were found``), so without a tsconfig it
    # would FALSE-BLOCK. No tsconfig → skip the tsc step (a pass, not a block).
    if not (cwd / "tsconfig.json").exists():
        return GateResult(
            passed=True,
            details="no tsconfig.json — skipping tsc (no type-check configured)",
        )

    # Resolve ``tsc`` from node_modules/.bin first (the project-pinned version),
    # falling back to ``npx tsc`` only when no local binary is installed.
    local_tsc = cwd / "node_modules" / ".bin" / "tsc"
    if local_tsc.exists():
        tsc_cmd = [str(local_tsc), "--noEmit"]
    else:
        tsc_cmd = ["npx", "tsc", "--noEmit"]
    return await _run_subprocess(tsc_cmd, cwd, timeout_s=timeout_s, tool_name="tsc")


def _is_virtual_workspace_manifest(cargo_toml: Path) -> bool:
    """Return True for a *virtual* workspace manifest ([workspace], no [package]).

    A virtual manifest has no package to build, so ``cargo check`` without
    ``--workspace`` errors out. Detection is a lightweight text scan (avoids a
    TOML dependency) for a ``[workspace]`` table header with no ``[package]``.
    """
    try:
        text = cargo_toml.read_text()
    except OSError:
        return False
    has_workspace = False
    has_package = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("[workspace]") or stripped.startswith("[workspace."):
            has_workspace = True
        elif stripped == "[package]" or stripped.startswith("[package."):
            has_package = True
    return has_workspace and not has_package


async def _run_cargo_check(cwd: Path, *, timeout_s: float) -> GateResult:
    # WS2-10: pass ``--workspace`` so a workspace-root repo checks every member
    # rather than false-blocking. A virtual manifest ([workspace] without
    # [package]) *requires* it; for an ordinary package it is harmless and still
    # checks the whole workspace it belongs to.
    args = ["cargo", "check", "--workspace"]
    if _is_virtual_workspace_manifest(cwd / "Cargo.toml"):
        tool_name = "cargo check (workspace)"
    else:
        tool_name = "cargo check"
    return await _run_subprocess(args, cwd, timeout_s=timeout_s, tool_name=tool_name)


async def _run_go_build(cwd: Path, *, timeout_s: float) -> GateResult:
    return await _run_subprocess(["go", "build", "./..."], cwd, timeout_s=timeout_s, tool_name="go build")


__all__ = ["run_build_check"]
