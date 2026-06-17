"""Holdout-set evaluation for tournament winners (v0.19.0).

The promotion-grade ladder treats a *repeated* winner as eligible for
promotion only if it also passes a **holdout** test set — the test files
that existed at the *baseline* commit (before the tournament started).
This catches winners that pass their own newly-introduced tests but
quietly regress pre-existing behavior.

Two-step protocol:

  1. :func:`extract_baseline_tests` — at tournament start, snapshot the
     ``tests/`` paths under the baseline commit via ``git ls-tree``.
  2. :func:`run_holdout_tests` — at the ``repeated → eligible``
     transition, run pytest restricted to those baseline paths.

The result feeds into :func:`tournament.promotion.decide` as the
``holdout_result`` argument: failure → ``no_change``, success →
``promote_to_eligible``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from qa.env import resolve_tool

logger = logging.getLogger(__name__)


_DEFAULT_TEST_DIR = "tests"
_PYTEST_TIMEOUT_S = 300

# Per-language test-discovery conventions. The holdout was historically
# pytest-only (``test_*.py`` / ``*_test.py``); a polyglot autonomous agent
# regresses Go/Rust/TS repos blind without these. Discovery is filename- or
# content-based (no toolchain invocation), so it degrades gracefully when the
# language's test runner isn't installed.
#
#   python : test_*.py / *_test.py  (existing pytest discovery rules)
#   go     : *_test.go              (``go test`` convention)
#   ts/js  : *.test.ts / *.test.tsx / *.test.js / *.spec.ts / *.spec.js
#   rust   : any .rs file containing a ``#[test]`` attribute
#
# Directories that never hold first-party tests are pruned to keep discovery
# cheap and to avoid counting vendored dependencies as "baseline tests".
_PRUNE_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        "target",  # rust build dir
        "vendor",  # go vendored deps
        "__pycache__",
        ".venv",
        "venv",
        ".tox",
        "dist",
        "build",
    }
)

_RUST_TEST_ATTR_RE = re.compile(r"#\[\s*test\s*\]")


def _is_python_test(name: str) -> bool:
    return name.endswith(".py") and (
        name.startswith("test_") or name.endswith("_test.py")
    )


def _is_go_test(name: str) -> bool:
    return name.endswith("_test.go")


def _is_ts_test(name: str) -> bool:
    return name.endswith(
        (
            ".test.ts",
            ".test.tsx",
            ".test.js",
            ".test.jsx",
            ".spec.ts",
            ".spec.tsx",
            ".spec.js",
            ".spec.jsx",
        )
    )


@dataclass(frozen=True)
class HoldoutResult:
    """Outcome of a holdout-test run.

    Attributes:
        passed: True iff every holdout test passed (or no holdout tests
            existed — vacuously passing).
        test_count: Number of test files attempted.
        failure_count: Number of test files that failed at least one
            test inside.
        failure_summary: Concatenation of the first lines of pytest
            failure summaries; capped to ~2KB for log hygiene.
    """

    passed: bool
    test_count: int
    failure_count: int
    failure_summary: str


async def extract_baseline_tests(
    cwd: Path,
    baseline_commit: str,
    test_dir: str = _DEFAULT_TEST_DIR,
) -> set[str]:
    """Return the set of repo-relative test paths present at *baseline_commit*.

    Uses ``git ls-tree -r --name-only <commit> <test_dir>`` — robust against
    ``ls-files`` (which is index-only, not commit-relative). Returns an
    empty set when the directory doesn't exist at that commit, the commit
    is invalid, or git is missing.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "ls-tree",
            "-r",
            "--name-only",
            baseline_commit,
            test_dir,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, _ = await proc.communicate()
    except FileNotFoundError:
        return set()
    except Exception as exc:  # noqa: BLE001
        logger.warning("holdout.git_ls_tree_error: %s", exc)
        return set()
    if proc.returncode != 0:
        return set()

    out: set[str] = set()
    for line in stdout_b.decode("utf-8", errors="replace").splitlines():
        path = line.strip()
        if not path:
            continue
        # Per-language test discovery (not pytest-only): python test_*.py /
        # *_test.py, go *_test.go, ts/js *.test.* / *.spec.*. Rust #[test]
        # discovery is content-based and lives in :func:`discover_holdout_scope`
        # (git ls-tree only yields names, not bodies).
        name = Path(path).name
        if _is_python_test(name) or _is_go_test(name) or _is_ts_test(name):
            out.add(path)
    return out


def _walk_files(root: Path) -> "list[Path]":
    """Yield all files under *root*, pruning known non-source directories."""
    out: list[Path] = []
    for path in root.rglob("*"):
        if any(part in _PRUNE_DIRS for part in path.relative_to(root).parts):
            continue
        if path.is_file():
            out.append(path)
    return out


def discover_tests_per_language(root: Path) -> dict[str, set[str]]:
    """Discover test files under *root* grouped by language.

    Per-language discovery (NOT pytest-only):

      * ``python``: ``test_*.py`` / ``*_test.py``
      * ``go``: ``*_test.go``
      * ``ts``: ``*.test.ts`` / ``*.spec.ts`` (+ tsx/js/jsx variants)
      * ``rust``: any ``.rs`` file containing a ``#[test]`` attribute

    Returns repo-relative POSIX paths keyed by language. Empty languages are
    omitted. Filename-based for python/go/ts (cheap); content-based for rust
    (``#[test]`` can live in any module).
    """
    by_lang: dict[str, set[str]] = {}
    for f in _walk_files(root):
        rel = f.relative_to(root).as_posix()
        name = f.name
        if _is_python_test(name):
            by_lang.setdefault("python", set()).add(rel)
        elif _is_go_test(name):
            by_lang.setdefault("go", set()).add(rel)
        elif _is_ts_test(name):
            by_lang.setdefault("ts", set()).add(rel)
        elif name.endswith(".rs"):
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if _RUST_TEST_ATTR_RE.search(text):
                by_lang.setdefault("rust", set()).add(rel)
    return by_lang


# Source-file extensions that mean "this scope contains real code worth
# regression-testing". Used by the non-vacuity check: a scope with source but
# ZERO discovered tests is a found-nothing-so-pass bug, not a vacuous pass.
_SOURCE_EXTS: frozenset[str] = frozenset(
    {".py", ".go", ".rs", ".ts", ".tsx", ".js", ".jsx", ".java", ".kt", ".rb"}
)


def _scope_has_source(root: Path) -> bool:
    for f in _walk_files(root):
        if f.suffix in _SOURCE_EXTS:
            return True
    return False


async def discover_holdout_scope(root: Path) -> HoldoutResult:
    """Discover holdout tests under *root*; fail LOUD on found-nothing.

    Non-vacuity contract (mirrors Group A's no-silent-dead-end signal):

      * Tests discovered (any language) → ``passed=True`` with the discovered
        ``test_count`` (this is a discovery probe, not an execution run).
      * NO tests discovered but the scope DOES contain source files →
        ``passed=False`` with a loud diagnostic. A gate that finds nothing in
        a non-empty scope must NOT silently pass — that is the canonical
        "the tool didn't actually run" failure mode.
      * Genuinely empty scope (no source, no tests) → vacuous ``passed=True``;
        an empty repo has nothing to regress.
    """
    by_lang = discover_tests_per_language(root)
    total = sum(len(v) for v in by_lang.values())
    if total > 0:
        langs = ",".join(sorted(by_lang))
        return HoldoutResult(
            passed=True,
            test_count=total,
            failure_count=0,
            failure_summary=f"discovered {total} test file(s) [{langs}]",
        )
    if _scope_has_source(root):
        # NON-VACUITY: source present, zero tests → loud, do not silent-pass.
        return HoldoutResult(
            passed=False,
            test_count=0,
            failure_count=0,
            failure_summary=(
                "holdout found NO tests in a non-empty scope "
                "(source files present, 0 test files discovered) — "
                "refusing to silently pass"
            ),
        )
    return HoldoutResult(
        passed=True,
        test_count=0,
        failure_count=0,
        failure_summary="empty scope — no source, no tests (vacuous pass)",
    )


_PYTEST_SUMMARY_RE = re.compile(
    r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE
)


def _parse_pytest_summary(stdout: str, attempted: set[str]) -> tuple[int, str]:
    """Count failed test files and build a summary."""
    failed_files: set[str] = set()
    for m in _PYTEST_SUMMARY_RE.finditer(stdout):
        path = m.group(1).split("::")[0]
        failed_files.add(path)

    if not failed_files:
        return 0, ""

    summary_lines: list[str] = []
    for path in sorted(failed_files):
        summary_lines.append(f"FAILED {path}")
    summary = "\n".join(summary_lines)
    if len(summary) > 2048:
        summary = summary[:2045] + "..."
    return len(failed_files & attempted) or len(failed_files), summary


async def run_holdout_tests(
    cwd: Path,
    baseline_test_paths: set[str],
    timeout_s: int = _PYTEST_TIMEOUT_S,
) -> HoldoutResult:
    """Run pytest on *baseline_test_paths*; return :class:`HoldoutResult`.

    Empty *baseline_test_paths* → vacuous pass (``test_count=0``).

    Each path is passed to pytest individually (``pytest path1 path2 …``).
    Files removed since the baseline are silently skipped — pytest
    reports them as collection errors but we don't count those as
    failures because the file no longer exists at HEAD.

    A subprocess timeout (>= ``timeout_s``) returns a failed result with a
    diagnostic summary — conservative; "we don't know" should not
    silently promote.
    """
    if not baseline_test_paths:
        return HoldoutResult(
            passed=True,
            test_count=0,
            failure_count=0,
            failure_summary="no baseline tests",
        )

    # Filter out files that no longer exist at HEAD.
    extant = {p for p in baseline_test_paths if (cwd / p).exists()}
    if not extant:
        return HoldoutResult(
            passed=True,
            test_count=0,
            failure_count=0,
            failure_summary="no baseline tests survive at HEAD",
        )

    # Run the target repo's own pytest (its .venv/bin/pytest → uv run → poetry
    # run → bare pytest) rather than a bare `python -m pytest` that may resolve
    # to an interpreter without pytest installed.
    base = resolve_tool(cwd, "pytest")
    args = [*base, "-q", "--no-header", *sorted(extant)]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, _stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_s
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return HoldoutResult(
                passed=False,
                test_count=len(extant),
                failure_count=len(extant),
                failure_summary=f"timeout after {timeout_s}s",
            )
    except FileNotFoundError:
        return HoldoutResult(
            passed=False,
            test_count=len(extant),
            failure_count=len(extant),
            failure_summary="pytest not found",
        )
    except Exception as exc:  # noqa: BLE001
        return HoldoutResult(
            passed=False,
            test_count=len(extant),
            failure_count=len(extant),
            failure_summary=f"pytest invocation error: {exc}",
        )

    stdout = stdout_b.decode("utf-8", errors="replace")
    failure_count, summary = _parse_pytest_summary(stdout, extant)

    return HoldoutResult(
        passed=(proc.returncode == 0),
        test_count=len(extant),
        failure_count=failure_count,
        failure_summary=summary,
    )


__all__ = [
    "HoldoutResult",
    "discover_holdout_scope",
    "discover_tests_per_language",
    "extract_baseline_tests",
    "run_holdout_tests",
]
