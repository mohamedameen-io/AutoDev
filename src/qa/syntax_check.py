"""Syntax-check gate.

Runs a language-appropriate syntax checker against the project and returns a
:class:`~plugins.registry.GateResult`.

Graceful degradation: if the required tool is not found, the gate passes with
an informational message rather than crashing the orchestrator.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from plugins.registry import GateResult
from qa._scan_filter import collect_scan_files
from qa.detect import detect_language
from qa.env import resolve_target_python


_DEFAULT_TIMEOUT_S = 60


async def run_syntax_check(
    cwd: Path,
    language: str | None = None,
    *,
    paths: list[Path] | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> GateResult:
    """Run a syntax check appropriate for *language* (auto-detected when ``None``).

    Returns a :class:`GateResult` with ``passed=True`` on success or when the
    required tool is not available (graceful degradation).

    G7 (submodule + generated exclude): the file walk excludes submodule trees
    (parsed from ``.gitmodules``), ``.git`` / ``.git/modules`` internals,
    vendored/cache dirs, and generated stubs (``*_pb2.py`` …) — a syntax error
    in any of those must not block an unrelated fix.

    S2 / WS2-16 (diff-scoping): when *paths* (repo-relative changed files) is
    given, the scan is scoped to those files instead of the whole tree
    (O(diff) instead of O(repo)). Generated files are skipped even when
    explicitly listed. ``paths=None`` preserves whole-tree behavior.
    """
    lang = language or detect_language(cwd)
    if lang is None:
        return GateResult(passed=True, details="language not detected, skipping syntax check")

    if lang == "python":
        return await _python_syntax_check(cwd, paths=paths, timeout_s=timeout_s)
    if lang == "nodejs":
        return await _nodejs_syntax_check(cwd, paths=paths, timeout_s=timeout_s)
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


async def _python_syntax_check(
    cwd: Path, *, paths: list[Path] | None = None, timeout_s: float
) -> GateResult:
    """Compile all .py files under *cwd* using ``python -m py_compile``.

    Files are split into batches of at most :data:`_PY_COMPILE_BATCH_SIZE`
    entries (or :data:`_PY_COMPILE_BATCH_BYTES` of total path length) to stay
    within the OS ``ARG_MAX`` limit on large projects.

    G7: submodule trees, ``.git``/``.git/modules``, vendored/cache dirs, and
    generated stubs (``*_pb2.py`` …) are excluded.
    S2: when *paths* is non-``None`` only those files are compiled.
    WS-6b: compiled with the *target* repo's interpreter (see
    :func:`qa.env.resolve_target_python`), not AutoDev's host ``sys.executable``
    — a version mismatch (host py3.13 vs a repo pinned to an older Python) would
    otherwise FALSE-FAIL ``py_compile`` on otherwise-clean code.
    """
    py_files = [str(p) for p in collect_scan_files(cwd, (".py",), paths=paths)]
    if not py_files:
        return GateResult(passed=True, details="no .py files found")

    interpreter = resolve_target_python(cwd)
    batches = _batch_files(py_files, _PY_COMPILE_BATCH_SIZE, _PY_COMPILE_BATCH_BYTES)
    all_errors: list[str] = []

    for batch in batches:
        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *interpreter,
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


# Plain-JavaScript extensions that ``node --check`` can parse directly.
_NODE_JS_EXTS = (".js", ".mjs", ".cjs")
# TypeScript / JSX extensions that ``node --check`` CANNOT parse (it rejects
# valid type annotations and JSX) — these need a TS-aware compiler.
_TS_EXTS = (".ts", ".tsx", ".jsx", ".mts", ".cts")


def _collect_node_files(
    cwd: Path, exts: tuple[str, ...], *, paths: list[Path] | None = None
) -> list[str]:
    """Return source files under *cwd* whose suffix is in *exts*.

    G7: submodule trees, ``.git``/``.git/modules``, vendored/cache dirs, and
    generated stubs are excluded via the shared filter.
    S2: when *paths* is non-``None`` only those files (matching *exts*) are
    returned.
    """
    return [str(p) for p in collect_scan_files(cwd, exts, paths=paths)]


def _resolve_tsc(cwd: Path) -> list[str] | None:
    """Return the argv prefix to invoke ``tsc`` for the repo at *cwd*, or ``None``.

    Cascade (first match wins):

    * ``node_modules/.bin/tsc`` exists → run the project-local compiler.
    * ``npx`` on PATH → ``npx --no-install tsc`` (uses a local/cached tsc only;
      never auto-installs, so the check is deterministic and offline-safe).
    * otherwise → ``None`` (no TS toolchain resolvable → caller degrades LOUD).
    """
    local_tsc = cwd / "node_modules" / ".bin" / "tsc"
    if local_tsc.exists():
        return [str(local_tsc)]
    if shutil.which("npx"):
        return ["npx", "--no-install", "tsc"]
    return None


async def _nodejs_syntax_check(
    cwd: Path, *, paths: list[Path] | None = None, timeout_s: float
) -> GateResult:
    """Syntax-check a Node/TypeScript project.

    Plain JavaScript (``.js``/``.mjs``/``.cjs``) is parsed with ``node --check``.
    TypeScript and JSX (``.ts``/``.tsx``/``.jsx``/``.mts``/``.cts``) cannot be
    parsed by ``node --check`` — Node rejects valid type annotations and JSX —
    so they are checked with ``tsc --noEmit``.

    WS2-8: a pure-TypeScript repo (only ``.ts`` files, no ``.js``) previously
    produced a *vacuous* "no .js files found" pass — a real TS syntax error
    sailed through undetected. Now TS files are caught, and when TS files exist
    but no ``tsc`` is resolvable the gate degrades LOUD (``passed=False`` with a
    ``skipped_toolchain_missing`` signal) rather than vacuous-passing.
    """
    js_files = _collect_node_files(cwd, _NODE_JS_EXTS, paths=paths)
    ts_files = _collect_node_files(cwd, _TS_EXTS, paths=paths)

    if not js_files and not ts_files:
        return GateResult(passed=True, details="no JS/TS files found")

    errors: list[str] = []
    checked = 0

    # --- Plain JavaScript via ``node --check`` --------------------------------
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

        checked += 1
        if proc.returncode != 0:
            errors.append(stderr.decode(errors="replace").strip())

    # --- TypeScript / JSX via ``tsc --noEmit`` --------------------------------
    if ts_files:
        tsc = _resolve_tsc(cwd)
        if tsc is None:
            # Phase-1B degrade-LOUD: a missing TS compiler is *unknown*, not
            # *clean* — never a vacuous pass for a repo that has .ts files.
            return GateResult(
                passed=False,
                details=(
                    f"{len(ts_files)} TypeScript file(s) present but no tsc resolvable: "
                    "syntax toolchain missing (skipped_toolchain_missing)"
                ),
                metrics={"skipped_toolchain_missing": True, "tool": "tsc"},
            )
        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *tsc,
                    "--noEmit",
                    *ts_files,
                    cwd=cwd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                timeout=timeout_s,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except FileNotFoundError:
            # npx/tsc binary vanished between resolve and exec → degrade LOUD.
            return GateResult(
                passed=False,
                details=(
                    f"{len(ts_files)} TypeScript file(s) present but tsc not runnable: "
                    "syntax toolchain missing (skipped_toolchain_missing)"
                ),
                metrics={"skipped_toolchain_missing": True, "tool": "tsc"},
            )
        except asyncio.TimeoutError:
            return GateResult(passed=False, details="syntax check timed out")

        checked += len(ts_files)
        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip() or stdout.decode(errors="replace").strip()
            errors.append(err)

    if errors:
        return GateResult(passed=False, details="syntax errors:\n" + "\n".join(errors))
    return GateResult(passed=True, details=f"syntax ok ({checked} files)")


__all__ = ["run_syntax_check"]
