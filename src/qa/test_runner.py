"""Test-runner gate.

Runs the project's test suite and returns a
:class:`~plugins.registry.GateResult`.

Non-vacuity contract (WS2-2, WS2-6)
-----------------------------------
A gate that passes because it ran NOTHING is a bug. This module fails LOUD in
two found-nothing worlds that used to silently green:

* **Zero tests in a non-empty scope** — pytest exits ``0`` printing
  *"no tests ran"* (asyncio/conftest plugins suppress exit-code 5), and
  ``go test`` exits ``0`` printing *"[no test files]"* when source exists but
  no test file does. When source IS present but the runner affirmatively ran
  zero tests, the gate now returns ``passed=False`` with a ``no_test_coverage``
  signal. The legitimate empty-source / empty-baseline fast-exits still pass.

* **Toolchain absent** — ``FileNotFoundError`` (the runner binary is not
  installed) used to be swallowed into a clean pass. It now degrades LOUD:
  ``passed=False`` with a ``skipped_toolchain_missing`` signal — *unknown*, not
  *clean*.
"""

from __future__ import annotations

import asyncio
import re
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


# Affirmative "ran nothing" tokens emitted by the runners on a zero-test run
# that *still exits 0*. Detection is affirmative on purpose: we only fail loud
# when the output positively says it ran/collected nothing. Unrecognised or
# empty output (e.g. a quiet runner) is treated as ``"unknown"`` and does NOT
# trigger the loud fail — that keeps quiet runners and mocked-empty-stdout
# results passing while still killing the concrete vacuous-pass vectors.
_ZERO_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bno tests ran\b", re.IGNORECASE),
    re.compile(r"\bno tests were run\b", re.IGNORECASE),
    re.compile(r"\b0 passed\b", re.IGNORECASE),
    re.compile(r"\bcollected 0 items\b", re.IGNORECASE),
    re.compile(r"\[no test files\]", re.IGNORECASE),  # go test
    re.compile(r"\bno test files\b", re.IGNORECASE),
)
# Positive "tests actually ran" tokens. Their presence overrides a coincidental
# zero-token (e.g. "0 failed, 5 passed").
_RAN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b([1-9]\d*) passed\b", re.IGNORECASE),
    re.compile(r"\b([1-9]\d*) failed\b", re.IGNORECASE),
    re.compile(r"\bcollected ([1-9]\d*) items?\b", re.IGNORECASE),
    re.compile(r"^ok\s", re.IGNORECASE | re.MULTILINE),  # go: "ok  pkg  0.5s"
    re.compile(r"\bok\b.*\bcoverage\b", re.IGNORECASE),
)


def _classify_run_count(output: str) -> tuple[str, int]:
    """Classify runner *output* as ``"ran"``, ``"zero"``, or ``"unknown"``.

    * ``"ran"`` — the output positively reports >=1 test executed/collected.
    * ``"zero"`` — the output affirmatively reports it ran/collected nothing.
    * ``"unknown"`` — no recognisable count token (quiet runner / empty stdout).

    A positive "ran" token wins over a zero token so ``"0 failed, 5 passed"``
    classifies as ``"ran"``.
    """
    if any(p.search(output) for p in _RAN_PATTERNS):
        return ("ran", 1)
    if any(p.search(output) for p in _ZERO_PATTERNS):
        return ("zero", 0)
    return ("unknown", -1)


def _has_source(cwd: Path, language: str | None) -> bool:
    """True when *cwd* holds non-trivial source for *language*.

    Used to distinguish a code-present-but-zero-tests repo (fail loud) from a
    genuinely empty / manifest-only repo (legitimate fast-exit pass). Skips
    virtualenv / cache dirs so a stray ``.venv`` does not count as source.
    """

    def _skip(p: Path) -> bool:
        parts = set(p.parts)
        return bool(parts & {".venv", "venv", "__pycache__", ".git", "node_modules"})

    suffixes: tuple[str, ...]
    if language == "python":
        suffixes = (".py",)
    elif language == "go":
        suffixes = (".go",)
    elif language == "nodejs":
        suffixes = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
    elif language == "rust":
        suffixes = (".rs",)
    else:
        suffixes = ()
    if not suffixes:
        return False
    for suffix in suffixes:
        for candidate in cwd.rglob(f"*{suffix}"):
            if not _skip(candidate):
                return True
    return False


async def _run_subprocess(
    args: list[str],
    cwd: Path,
    *,
    timeout_s: float,
    tool_name: str,
    scope_nonempty: bool = False,
) -> GateResult:
    """Run *args*; return a GateResult.

    When *scope_nonempty* is True and the runner exits 0 but affirmatively ran
    zero tests, fail LOUD (``no_test_coverage``) instead of vacuously passing.
    A missing toolchain (``FileNotFoundError``) degrades LOUD
    (``skipped_toolchain_missing``).
    """
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
        # WS2-6: a missing toolchain is *unknown*, not *clean*. Degrade loud so
        # the resolver treats it as blocking rather than a silent green.
        return GateResult(
            passed=False,
            details=(
                f"{tool_name} not installed: test toolchain missing "
                "(skipped_toolchain_missing)"
            ),
            metrics={"skipped_toolchain_missing": True, "tool": tool_name},
        )
    except asyncio.TimeoutError:
        return GateResult(passed=False, details=f"{tool_name} tests timed out")

    combined = (stdout + stderr).decode(errors="replace").strip()
    if proc.returncode == 0:
        # WS2-2: rc==0 alone is not a pass. If source is present but the runner
        # affirmatively ran zero tests, this is a vacuous pass → fail loud.
        verdict, _ = _classify_run_count(combined)
        if scope_nonempty and verdict == "zero":
            return GateResult(
                passed=False,
                details=(
                    f"{tool_name}: 0 tests ran in a non-empty source scope "
                    "(no_test_coverage / scope mismatch — no tests found)"
                ),
                metrics={"no_test_coverage": True, "tool": tool_name},
            )
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
    # Non-empty scope = the repo actually has python source. A manifest-only
    # repo (no ``.py``) keeps the legitimate "nothing to cover yet" fast-exit
    # pass; a repo WITH code but zero tests fails loud.
    scope_nonempty = _has_source(cwd, "python")
    return await _run_subprocess(
        args, cwd, timeout_s=timeout_s, tool_name="pytest", scope_nonempty=scope_nonempty
    )


async def _run_npm_test(cwd: Path, *, timeout_s: float) -> GateResult:
    return await _run_subprocess(
        ["npm", "test"],
        cwd,
        timeout_s=timeout_s,
        tool_name="npm test",
        scope_nonempty=_has_source(cwd, "nodejs"),
    )


async def _run_cargo_test(cwd: Path, *, timeout_s: float) -> GateResult:
    return await _run_subprocess(
        ["cargo", "test"],
        cwd,
        timeout_s=timeout_s,
        tool_name="cargo test",
        scope_nonempty=_has_source(cwd, "rust"),
    )


async def _run_go_test(cwd: Path, *, timeout_s: float) -> GateResult:
    return await _run_subprocess(
        ["go", "test", "./..."],
        cwd,
        timeout_s=timeout_s,
        tool_name="go test",
        scope_nonempty=_has_source(cwd, "go"),
    )


__all__ = ["run_tests"]
