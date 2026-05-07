"""Tests for ``autodev tournament promote`` (v0.6.0 / Issue 2 salvage CLI).

The ``promote`` subcommand reads a tournament's persisted incumbent
markdown from disk and writes it into the local ``.autodev/plan.json``
without re-running the tournament. It supports two modes:

  - default: latest incumbent (highest ``incumbent_after_NN.md``).
  - explicit: ``--pass N`` selects a specific incumbent by pass number.

These tests exercise the CLI surface end-to-end via Click's CliRunner,
in an isolated tmp filesystem so plan.json writes are sandboxed.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from cli import cli


SALVAGE_INCUMBENT_3_MD = """# Plan: Salvaged at pass 3

## Phase 1: Implement

### Task 1.1: pass-3 task
  - Description: from incumbent_after_03.md
  - Files: foo.py
  - Acceptance:
    - [ ] pass-3 marker present
"""

SALVAGE_INCUMBENT_5_MD = """# Plan: Salvaged at pass 5

## Phase 1: Implement

### Task 1.1: pass-5 task
  - Description: from incumbent_after_05.md
  - Files: foo.py
  - Acceptance:
    - [ ] pass-5 marker present
"""


def _seed_tournament_dir(fs_root: Path, tournament_id: str) -> Path:
    """Create ``.autodev/tournaments/<tournament_id>/`` with two incumbents."""
    artifact_dir = fs_root / ".autodev" / "tournaments" / tournament_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "incumbent_after_03.md").write_text(
        SALVAGE_INCUMBENT_3_MD, encoding="utf-8"
    )
    (artifact_dir / "incumbent_after_05.md").write_text(
        SALVAGE_INCUMBENT_5_MD, encoding="utf-8"
    )
    return artifact_dir


def test_tournament_promote_uses_latest_incumbent(tmp_path: Path) -> None:
    """``promote --tournament-id X`` (no --pass) uses the highest pass."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as fs_dir:
        fs_root = Path(fs_dir)
        _seed_tournament_dir(fs_root, "plan-deadbeef")

        result = runner.invoke(
            cli,
            ["tournament", "promote", "--tournament-id", "plan-deadbeef"],
        )
        assert result.exit_code == 0, (
            f"exit={result.exit_code}\noutput:\n{result.output}\nexc:{result.exception!r}"
        )
        assert "pass=5" in result.output or "pass 5" in result.output

        plan_json = fs_root / ".autodev" / "plan.json"
        assert plan_json.exists(), result.output
        plan_text = plan_json.read_text(encoding="utf-8")
        assert "Salvaged at pass 5" in plan_text


def test_tournament_promote_explicit_pass(tmp_path: Path) -> None:
    """``promote --tournament-id X --pass 3`` uses the requested pass."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as fs_dir:
        fs_root = Path(fs_dir)
        _seed_tournament_dir(fs_root, "plan-deadbeef")

        result = runner.invoke(
            cli,
            [
                "tournament",
                "promote",
                "--tournament-id",
                "plan-deadbeef",
                "--pass",
                "3",
            ],
        )
        assert result.exit_code == 0, (
            f"exit={result.exit_code}\noutput:\n{result.output}\nexc:{result.exception!r}"
        )
        plan_text = (fs_root / ".autodev" / "plan.json").read_text(encoding="utf-8")
        assert "Salvaged at pass 3" in plan_text
        assert "Salvaged at pass 5" not in plan_text


def test_tournament_promote_missing_id_fails_2(tmp_path: Path) -> None:
    """``promote`` without ``--tournament-id`` exits with code 2."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(cli, ["tournament", "promote"])
    assert result.exit_code == 2


def test_tournament_promote_missing_incumbent_fails_2(tmp_path: Path) -> None:
    """``promote --tournament-id <unknown>`` (no on-disk incumbents) exits 2."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as fs_dir:
        fs_root = Path(fs_dir)
        # Create the dir but DON'T seed any incumbents.
        adir = fs_root / ".autodev" / "tournaments" / "plan-empty01"
        adir.mkdir(parents=True, exist_ok=True)

        result = runner.invoke(
            cli, ["tournament", "promote", "--tournament-id", "plan-empty01"]
        )
        assert result.exit_code == 2, result.output


def test_tournament_promote_explicit_missing_pass_fails_2(tmp_path: Path) -> None:
    """``promote --pass N`` for a pass number that doesn't exist exits 2."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as fs_dir:
        fs_root = Path(fs_dir)
        _seed_tournament_dir(fs_root, "plan-deadbeef")

        result = runner.invoke(
            cli,
            [
                "tournament",
                "promote",
                "--tournament-id",
                "plan-deadbeef",
                "--pass",
                "99",
            ],
        )
        assert result.exit_code == 2, result.output


# ── Backward-compat regression: existing flat-command invocations work ─────


SAMPLE_PLAN_MD = """# Plan: CLI sample

## Phase 1: Do stuff

### Task 1.1: Write code
  - Description: Write simple code.
  - Files: foo.py
  - Acceptance:
    - [ ] compiles
"""


def test_tournament_run_still_works_after_group_refactor(tmp_path: Path) -> None:
    """Regression: existing ``autodev tournament --phase=plan`` invocations
    must continue to work even after the ``promote`` subcommand was added.
    The CLI restructure adds ``run`` as a synonymous subcommand but the
    flat-flag form remains supported for backward compat.
    """
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as fs_dir:
        fs_root = Path(fs_dir)
        input_md = fs_root / "sample_plan.md"
        input_md.write_text(SAMPLE_PLAN_MD, encoding="utf-8")

        # Original flat-flag form: ``tournament --phase=plan ...``
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

    assert result.exit_code == 0, (
        f"exit={result.exit_code}\noutput:\n{result.output}\nexc:{result.exception!r}"
    )
    assert "Tournament complete" in result.output
