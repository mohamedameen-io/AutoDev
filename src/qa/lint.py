"""Lint gate.

Runs the detected linter for the project and returns a
:class:`~plugins.registry.GateResult`.

Toolchain-absent degrade-LOUD (WS2-6)
-------------------------------------
A missing linter binary is *unknown*, not *clean*. When the linter is absent
(``FileNotFoundError``) this gate now returns ``passed=False`` with a
``skipped_toolchain_missing`` signal instead of silently passing.

ESLint config-absent skip (stabilization-v1)
--------------------------------------------
ESLint v9+ requires an ``eslint.config.js`` (or equivalent) and exits non-zero
with a setup error when none is found.  That is not a code violation — it is an
environment condition.  When no eslint config is detected in *cwd*, the gate
returns ``passed=True`` with ``skipped_lint_no_config=True`` (mirroring the
language/linter-absent skip pattern already used here).

Tool setup / env errors (stabilization-v1)
------------------------------------------
ENOENT (tool not on PATH) and ESLint's "couldn't find a configuration file"
output are environment failures, not code violations.  These are now classified
as ``severity="warn"`` (``passed=True``) so the pipeline continues and the
issue is surfaced as a non-blocking advisory.  Genuine lint violations
(non-zero exit without any setup-error signal) remain ``severity="block"``.
Note: the ESLint config-absent and tool-env-error cases are intentional
exceptions to the "unconditional passed=False on tool errors" rule — they
degrade to skip/warn rather than block, by design.
"""

from __future__ import annotations

import asyncio
import json
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
        # WS2-6: degrade loud — a missing linter is unknown, not clean.
        return GateResult(
            passed=False,
            details=(
                f"{tool_name} not installed: lint toolchain missing "
                "(skipped_toolchain_missing)"
            ),
            metrics={"skipped_toolchain_missing": True, "tool": tool_name},
        )
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


def _has_eslint_config(cwd: Path) -> bool:
    """Return True if *cwd* contains an ESLint configuration.

    Checks for:
    * ``eslint.config.{js,mjs,cjs,ts,mts,cts}`` (ESLint v9+ flat config; TS
      variants are supported by ESLint 9.10+)
    * ``.eslintrc``, ``.eslintrc.{js,cjs,yaml,yml,json}`` (legacy)
    * ``package.json`` with an ``"eslintConfig"`` key
    """
    flat_configs = (
        "eslint.config.js",
        "eslint.config.mjs",
        "eslint.config.cjs",
        "eslint.config.ts",
        "eslint.config.mts",
        "eslint.config.cts",
    )
    for name in flat_configs:
        if (cwd / name).is_file():
            return True

    legacy_configs = (
        ".eslintrc",
        ".eslintrc.js",
        ".eslintrc.cjs",
        ".eslintrc.yaml",
        ".eslintrc.yml",
        ".eslintrc.json",
    )
    for name in legacy_configs:
        if (cwd / name).is_file():
            return True

    pkg = cwd / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "eslintConfig" in data:
                return True
        except (OSError, json.JSONDecodeError):
            pass

    return False


# stabilization-v1: ESLint v9+ emits one of these *startup* errors when it cannot
# find / load its config.  These are environment conditions, not code violations.
#
# IMPORTANT: every signal is tightly anchored to ESLint's actual config-not-found
# startup message — a find/locate verb paired with a config noun, or the literal
# "no eslint configuration found".  We deliberately do NOT match bare substrings
# like "configuration file", "eslint config", or "no config": a genuine lint
# violation whose rule advisory merely *contains* such a phrase (e.g. "specify
# 'env.node: true' in your configuration file") must stay passed=False/block and
# never be demoted to a non-blocking warn.
_ESLINT_SETUP_ERROR_SIGNALS = (
    "couldn't find a configuration file",
    "couldn't find an eslint.config",
    "could not find a configuration file",
    "could not find an eslint.config",
    "could not find the config file",
    "no eslint configuration found",
)


async def _run_eslint(cwd: Path, *, timeout_s: float) -> GateResult:
    # stabilization-v1: pre-check for eslint config.  ESLint v9+ exits non-zero
    # with a setup error when none is found; that is not a code violation.
    # Mirror the language/linter-absent skip pattern used elsewhere in this file.
    if not _has_eslint_config(cwd):
        return GateResult(
            passed=True,
            details="eslint: no config file found, skipping lint",
            metrics={"skipped_lint_no_config": True},
        )
    # Prefer local npx eslint; fall back gracefully.
    result = await _run_subprocess(["npx", "eslint", "."], cwd, timeout_s=timeout_s, tool_name="eslint")

    if not result.passed:
        # stabilization-v1: re-classify ESLint tool/env failures as warn (non-blocking).
        # ENOENT (npx/eslint not on PATH) or ESLint config-not-found output are
        # environment conditions, not code violations — surface as advisory, continue.
        is_toolchain_missing = result.metrics.get("skipped_toolchain_missing", False)
        combined_lower = result.details.lower()
        is_setup_error = any(sig in combined_lower for sig in _ESLINT_SETUP_ERROR_SIGNALS)
        if is_toolchain_missing or is_setup_error:
            return GateResult(
                passed=True,
                severity="warn",
                details=result.details,
                metrics={**result.metrics, "lint_setup_error": True},
            )

    return result


async def _run_cargo_clippy(cwd: Path, *, timeout_s: float) -> GateResult:
    # WS2-10: ``--workspace`` lints every member crate, not just the package in
    # ``cwd``. A lone-package (non-workspace) repo is treated by cargo as a
    # one-member workspace, so the flag is safe to pass unconditionally.
    return await _run_subprocess(
        ["cargo", "clippy", "--workspace"], cwd, timeout_s=timeout_s, tool_name="cargo clippy"
    )


async def _run_golangci_lint(cwd: Path, *, timeout_s: float) -> GateResult:
    return await _run_subprocess(["golangci-lint", "run"], cwd, timeout_s=timeout_s, tool_name="golangci-lint")


__all__ = ["run_lint"]
