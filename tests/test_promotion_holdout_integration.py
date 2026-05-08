"""Integration tests for v0.19.0 promotion-grade + holdout flow."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from plugins.registry import GateResult
from tournament.holdout import (
    HoldoutResult,
    extract_baseline_tests,
    run_holdout_tests,
)
from tournament.promotion import decide


def _passing_gates() -> list[GateResult]:
    return [GateResult(passed=True, details="ok")]


def _init_repo_with_tests(tmp_path: Path, contents: str) -> str:
    """Build a baseline with one test file containing *contents*. Returns commit hash."""
    subprocess.check_call(("git", "init", "-q"), cwd=tmp_path)
    subprocess.check_call(
        ("git", "config", "user.email", "t@example.com"), cwd=tmp_path
    )
    subprocess.check_call(("git", "config", "user.name", "test"), cwd=tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_baseline.py").write_text(contents)
    subprocess.check_call(("git", "add", "."), cwd=tmp_path)
    subprocess.check_call(("git", "commit", "-q", "-m", "baseline"), cwd=tmp_path)
    return (
        subprocess.check_output(
            ("git", "rev-parse", "HEAD"), cwd=tmp_path, text=True
        ).strip()
    )


@pytest.mark.asyncio
async def test_full_holdout_pass_promotes(tmp_path: Path) -> None:
    """Baseline tests pass at HEAD → promote_to_eligible."""
    commit = _init_repo_with_tests(tmp_path, "def test_a(): assert True\n")

    paths = await extract_baseline_tests(tmp_path, commit)
    assert paths

    holdout = await run_holdout_tests(tmp_path, paths)
    assert holdout.passed

    decision = decide("repeated", _passing_gates(), holdout_result=holdout)
    assert decision.action == "promote_to_eligible"


@pytest.mark.asyncio
async def test_full_holdout_fail_blocks_promotion(tmp_path: Path) -> None:
    """Baseline tests fail at HEAD → no_change."""
    commit = _init_repo_with_tests(
        tmp_path,
        "def test_a(): assert False\n",
    )
    paths = await extract_baseline_tests(tmp_path, commit)
    assert paths

    holdout = await run_holdout_tests(tmp_path, paths)
    assert not holdout.passed

    decision = decide("repeated", _passing_gates(), holdout_result=holdout)
    assert decision.action == "no_change"
    assert "holdout" in decision.reason


@pytest.mark.asyncio
async def test_holdout_with_dev_best_grade_ignored(tmp_path: Path) -> None:
    """Holdout argument is only consulted at the ``repeated`` rung."""
    failing = HoldoutResult(
        passed=False, test_count=1, failure_count=1, failure_summary="x"
    )
    decision = decide("dev_best", _passing_gates(), holdout_result=failing)
    # dev_best with no kill_rate → demand_repeat (not no_change).
    assert decision.action == "demand_repeat"


@pytest.mark.asyncio
async def test_holdout_empty_baseline_does_not_block(tmp_path: Path) -> None:
    """Repos without baseline tests vacuously pass and promote."""
    holdout = await run_holdout_tests(tmp_path, set())
    assert holdout.passed
    decision = decide("repeated", _passing_gates(), holdout_result=holdout)
    assert decision.action == "promote_to_eligible"
