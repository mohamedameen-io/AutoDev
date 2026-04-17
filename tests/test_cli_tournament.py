"""Tests for ``autodev tournament`` CLI command (Phase 6 / Phase 7).

Dry-run mode uses a canned LLM client so no subprocess is spawned. The
``impl`` subcommand was a stub until Phase 7; it is now fully implemented
and requires ``--task-id`` and ``--task-desc`` arguments.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from cli import cli


SAMPLE_PLAN_MD = """# Plan: CLI sample

## Phase 1: Do stuff

### Task 1.1: Write code
  - Description: Write simple code.
  - Files: foo.py
  - Acceptance:
    - [ ] compiles
"""


# ── Error / argument-handling tests ──────────────────────────────────────


def test_tournament_impl_phase_requires_task_id() -> None:
    """``--phase=impl`` without ``--task-id`` exits 2 (missing required option)."""
    runner = CliRunner()
    result = runner.invoke(cli, ["tournament", "--phase=impl"])
    assert result.exit_code == 2


def test_tournament_plan_missing_input_errors_cleanly(tmp_path: Path) -> None:
    """``--phase=plan`` without ``--input`` errors with exit code 2."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(cli, ["tournament", "--phase=plan"])
    assert result.exit_code == 2
    assert "--input is required" in result.output


def test_tournament_plan_nonexistent_input_errors(tmp_path: Path) -> None:
    """A missing input file surfaces a clear error, not a traceback."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(
            cli, ["tournament", "--phase=plan", "--input", "does-not-exist.md"]
        )
    assert result.exit_code == 2
    assert "not found" in result.output


# ── Dry-run success path ─────────────────────────────────────────────────


def test_tournament_plan_dry_run_succeeds(tmp_path: Path) -> None:
    """``--dry-run`` runs the full tournament offline and writes artifacts."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as fs_dir:
        fs_root = Path(fs_dir)
        input_md = fs_root / "sample_plan.md"
        input_md.write_text(SAMPLE_PLAN_MD, encoding="utf-8")

        result = runner.invoke(
            cli,
            [
                "tournament",
                "--phase=plan",
                "--input",
                str(input_md),
                "--dry-run",
                "--max-rounds",
                "2",
            ],
        )

    assert result.exit_code == 0, (
        f"exit={result.exit_code}\noutput:\n{result.output}\nexc: {result.exception!r}"
    )
    # Summary output present.
    assert "Tournament complete" in result.output
    assert "final_bytes=" in result.output


def test_tournament_plan_dry_run_writes_artifacts(tmp_path: Path) -> None:
    """Artifacts land under ``.autodev/tournaments/plan-*/`` in the cwd."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as fs_dir:
        fs_root = Path(fs_dir)
        input_md = fs_root / "plan.md"
        input_md.write_text(SAMPLE_PLAN_MD, encoding="utf-8")

        result = runner.invoke(
            cli,
            [
                "tournament",
                "--phase=plan",
                "--input",
                str(input_md),
                "--dry-run",
                "--max-rounds",
                "1",
            ],
        )
        assert result.exit_code == 0, result.output

        tournaments_root = fs_root / ".autodev" / "tournaments"
        assert tournaments_root.exists(), (
            f"no .autodev/tournaments dir created; output:\n{result.output}"
        )
        plan_dirs = [d for d in tournaments_root.iterdir() if d.name.startswith("plan-")]
        assert len(plan_dirs) == 1
        adir = plan_dirs[0]
        assert (adir / "initial_a.md").exists()
        assert (adir / "final_output.md").exists()
        assert (adir / "history.json").exists()


def test_tournament_plan_dry_run_without_project_uses_defaults(tmp_path: Path) -> None:
    """No ``.autodev/config.json`` + ``--dry-run`` falls back to default config."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as fs_dir:
        input_md = Path(fs_dir) / "plan.md"
        input_md.write_text(SAMPLE_PLAN_MD, encoding="utf-8")
        result = runner.invoke(
            cli,
            [
                "tournament",
                "--phase=plan",
                "--input",
                str(input_md),
                "--dry-run",
                "--max-rounds",
                "1",
            ],
        )
    assert result.exit_code == 0, result.output
    assert "using defaults" in result.output.lower()


def test_tournament_help_lists_phases() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["tournament", "--help"])
    assert result.exit_code == 0
    assert "--phase" in result.output
    assert "plan" in result.output
    assert "impl" in result.output
    assert "--dry-run" in result.output
