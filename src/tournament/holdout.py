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

logger = logging.getLogger(__name__)


_DEFAULT_TEST_DIR = "tests"
_PYTEST_TIMEOUT_S = 300


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
        # Restrict to .py test files; common pytest discovery rules.
        if not path.endswith(".py"):
            continue
        if not (
            Path(path).name.startswith("test_") or Path(path).name.endswith("_test.py")
        ):
            continue
        out.add(path)
    return out


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

    args = ["python", "-m", "pytest", "-q", "--no-header", *sorted(extant)]
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
    "extract_baseline_tests",
    "run_holdout_tests",
]
