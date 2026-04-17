"""Tests for ``autodev tournament --phase=impl`` CLI command."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from cli.commands.tournament import tournament


def _write_diff(path: Path, content: str = "+def foo(): pass\n") -> None:
    path.write_text(content, encoding="utf-8")


def test_impl_phase_requires_diff_file(tmp_path: Path) -> None:
    """``--phase=impl`` without --input-diff or --input exits with code 2."""
    runner = CliRunner()
    result = runner.invoke(tournament, ["--phase=impl", "--dry-run"])
    assert result.exit_code == 2
    assert "required" in result.output.lower() or "input" in result.output.lower()


def test_impl_phase_nonexistent_diff_file_exits(tmp_path: Path) -> None:
    """``--phase=impl`` with a nonexistent diff file exits with code 2."""
    runner = CliRunner()
    result = runner.invoke(
        tournament,
        ["--phase=impl", "--input-diff", str(tmp_path / "nonexistent.diff"), "--dry-run"],
    )
    assert result.exit_code == 2


def test_impl_phase_dry_run_succeeds(tmp_path: Path) -> None:
    """``--phase=impl --dry-run`` with a valid diff file runs to completion."""
    diff_file = tmp_path / "test.diff"
    _write_diff(diff_file)

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(
            tournament,
            [
                "--phase=impl",
                "--input-diff",
                str(diff_file),
                "--task-desc",
                "Add foo()",
                "--dry-run",
                "--max-rounds",
                "1",
            ],
        )
    assert result.exit_code == 0, f"output: {result.output}"
    assert "Tournament complete" in result.output
    assert "final_diff_bytes=" in result.output


def test_impl_phase_dry_run_with_input_flag(tmp_path: Path) -> None:
    """``--phase=impl --input`` (not --input-diff) also works as diff source."""
    diff_file = tmp_path / "test.diff"
    _write_diff(diff_file)

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(
            tournament,
            [
                "--phase=impl",
                "--input",
                str(diff_file),
                "--task-desc",
                "Add foo()",
                "--dry-run",
                "--max-rounds",
                "1",
            ],
        )
    assert result.exit_code == 0, f"output: {result.output}"
    assert "Tournament complete" in result.output


def test_impl_phase_dry_run_with_files_option(tmp_path: Path) -> None:
    """``--files`` option is accepted and passed through."""
    diff_file = tmp_path / "test.diff"
    _write_diff(diff_file)

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(
            tournament,
            [
                "--phase=impl",
                "--input-diff",
                str(diff_file),
                "--task-desc",
                "Add foo()",
                "--files",
                "foo.py,bar.py",
                "--dry-run",
                "--max-rounds",
                "1",
            ],
        )
    assert result.exit_code == 0, f"output: {result.output}"


def test_plan_phase_still_works_after_impl_changes(tmp_path: Path) -> None:
    """``--phase=plan`` still works after the impl changes (regression guard)."""
    plan_file = tmp_path / "plan.md"
    plan_file.write_text(
        "# Plan: Add foo\n\n## Phase 1: Implement\n\n### Task 1.1: Write foo\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(
            tournament,
            [
                "--phase=plan",
                "--input",
                str(plan_file),
                "--dry-run",
                "--max-rounds",
                "1",
            ],
        )
    assert result.exit_code == 0, f"output: {result.output}"
    assert "Tournament complete" in result.output
