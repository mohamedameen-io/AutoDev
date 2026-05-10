"""Integration test: end-to-end ``calibrate_minimality_judge.py`` invocation.

This test runs the calibration script as a subprocess against the v1
synthetic gold corpus and asserts the script exits 0. It is marked
``integration`` so the default test suite (``uv run pytest tests/``)
does not run it; CI exercises it via the integration target.

When the real judge wiring lands (``--judge-cmd``), this test will be
extended to exercise the live path under ``AUTODEV_LIVE=1``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "calibrate_minimality_judge.py"
_GOLD = (
    _REPO_ROOT
    / "tests"
    / "calibration"
    / "minimality_judge"
    / "gold_rankings.jsonl"
)


pytestmark = pytest.mark.integration


def test_calibration_script_exits_zero_on_synthetic_corpus() -> None:
    """Subprocess invocation must succeed on the v1 synthetic corpus.

    Uses the current Python interpreter directly (no ``uv run``) so the
    test does not depend on the project's package manager being on PATH
    in the integration runner.
    """
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "--gold", str(_GOLD)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"calibration script exited {proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "All criteria pass: **YES**" in proc.stdout


def test_calibration_script_jsonl_report_is_machine_readable() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--gold",
            str(_GOLD),
            "--report",
            "jsonl",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    # Smoke: the script's headline metric is present and well-typed.
    assert "aggregate_spearman" in payload
    assert isinstance(payload["aggregate_spearman"], float)
    # Borda diagnostic carries the int-cast finding through.
    assert payload["borda_diagnostic"]["int_collapse_top_two"] is True
