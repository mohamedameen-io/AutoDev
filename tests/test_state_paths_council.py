"""v0.18.0 C2: tests for state.paths.council_criteria_path."""

from __future__ import annotations

from pathlib import Path

from state.paths import council_criteria_path


def test_council_criteria_path(tmp_path: Path) -> None:
    p = council_criteria_path(tmp_path, "1.1")
    assert p == tmp_path / ".autodev" / "tournaments" / "council-1.1.json"


def test_council_criteria_path_safe_task_id(tmp_path: Path) -> None:
    """Slashes and spaces in task_id are normalized to underscores."""
    p = council_criteria_path(tmp_path, "phase 1/task 2")
    assert p == tmp_path / ".autodev" / "tournaments" / "council-phase_1_task_2.json"
