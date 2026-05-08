"""Tests for v0.19.0 tournament/holdout module."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tournament.holdout import (
    HoldoutResult,
    _parse_pytest_summary,
    extract_baseline_tests,
    run_holdout_tests,
)


def _git(*args: str, cwd: Path) -> str:
    return subprocess.check_output(
        ("git",) + args,
        cwd=cwd,
        text=True,
    ).strip()


def _init_repo(tmp_path: Path) -> str:
    """Initialise a git repo with a baseline tests/ tree. Returns commit hash."""
    subprocess.check_call(("git", "init", "-q"), cwd=tmp_path)
    subprocess.check_call(
        ("git", "config", "user.email", "t@example.com"), cwd=tmp_path
    )
    subprocess.check_call(("git", "config", "user.name", "test"), cwd=tmp_path)

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_alpha.py").write_text(
        "def test_alpha(): assert 1 == 1\n"
    )
    (tmp_path / "tests" / "test_beta.py").write_text(
        "def test_beta(): assert 2 == 2\n"
    )
    (tmp_path / "tests" / "helpers.py").write_text(
        "def helper(): return 1\n"
    )
    subprocess.check_call(("git", "add", "."), cwd=tmp_path)
    subprocess.check_call(
        ("git", "commit", "-q", "-m", "baseline"), cwd=tmp_path
    )
    return _git("rev-parse", "HEAD", cwd=tmp_path)


@pytest.mark.asyncio
async def test_extract_baseline_tests_finds_test_files(tmp_path: Path) -> None:
    commit = _init_repo(tmp_path)
    paths = await extract_baseline_tests(tmp_path, commit)
    assert "tests/test_alpha.py" in paths
    assert "tests/test_beta.py" in paths
    # Helper file is not a test → excluded.
    assert "tests/helpers.py" not in paths


@pytest.mark.asyncio
async def test_extract_baseline_tests_invalid_commit_returns_empty(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    paths = await extract_baseline_tests(tmp_path, "deadbeef" * 5)
    assert paths == set()


@pytest.mark.asyncio
async def test_extract_baseline_tests_missing_dir_returns_empty(
    tmp_path: Path,
) -> None:
    """A baseline that lacked a ``tests/`` dir returns an empty set."""
    subprocess.check_call(("git", "init", "-q"), cwd=tmp_path)
    subprocess.check_call(
        ("git", "config", "user.email", "t@example.com"), cwd=tmp_path
    )
    subprocess.check_call(("git", "config", "user.name", "test"), cwd=tmp_path)
    (tmp_path / "src.py").write_text("x = 1\n")
    subprocess.check_call(("git", "add", "."), cwd=tmp_path)
    subprocess.check_call(("git", "commit", "-q", "-m", "init"), cwd=tmp_path)
    commit = _git("rev-parse", "HEAD", cwd=tmp_path)
    paths = await extract_baseline_tests(tmp_path, commit)
    assert paths == set()


@pytest.mark.asyncio
async def test_run_holdout_tests_empty_baseline_passes(tmp_path: Path) -> None:
    result = await run_holdout_tests(tmp_path, set())
    assert result.passed
    assert result.test_count == 0


@pytest.mark.asyncio
async def test_run_holdout_tests_missing_files_passes(tmp_path: Path) -> None:
    """A baseline path that no longer exists at HEAD doesn't fail the holdout."""
    paths = {"tests/never_existed.py"}
    result = await run_holdout_tests(tmp_path, paths)
    assert result.passed
    assert "no baseline tests survive" in result.failure_summary


def test_parse_pytest_summary_no_failures() -> None:
    out = """============================= test session starts ==============================
collected 5 items

tests/test_alpha.py .....                                                  [100%]

============================== 5 passed in 0.05s ==============================
"""
    failed_count, summary = _parse_pytest_summary(out, {"tests/test_alpha.py"})
    assert failed_count == 0
    assert summary == ""


def test_parse_pytest_summary_with_failure() -> None:
    out = """============================= FAILURES ===============================
FAILED tests/test_alpha.py::test_thing - AssertionError
=========================== short test summary ==============================
FAILED tests/test_alpha.py::test_thing - AssertionError
============================== 1 failed in 0.05s ===========================
"""
    failed_count, summary = _parse_pytest_summary(out, {"tests/test_alpha.py"})
    assert failed_count == 1
    assert "tests/test_alpha.py" in summary


def test_holdout_result_dataclass_construction() -> None:
    r = HoldoutResult(
        passed=True, test_count=10, failure_count=0, failure_summary=""
    )
    assert r.passed
    assert r.test_count == 10
