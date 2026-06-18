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

import json
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


# ── F-1 bypass fix: promote path applies repair_phase_edit_scope ─────────────

# A salvaged plan where the phase EDIT_SCOPE only lists ``index.js``, but
# Task 1.2 declares ``test_index.js``.  Without the fix the promoted plan.json
# would contain Phase 1 edit_scope = ["index.js"], and the pre-flight check
# would raise edit_scope_violation for ``test_index.js`` at execute time.
# With the fix the promote _persist path calls repair_phase_edit_scope before
# init_plan, so ``test_index.js`` is admitted into the phase scope.
_SALVAGE_NARROWED_SCOPE_MD = """# Plan: Salvaged narrow scope

## Phase 1: Implement
EDIT_SCOPE:
  - index.js

### Task 1.1: Implement the feature
  - Description: add the feature to index.js
  - Files: index.js
  - Acceptance:
    - [ ] feature works

### Task 1.2: Add tests
  - Description: cover the new feature with tests
  - Files: test_index.js
  - Acceptance:
    - [ ] tests pass
"""


def test_tournament_promote_repairs_phase_edit_scope(tmp_path: Path) -> None:
    """F-1 bypass fix: a salvaged plan whose phase ``edit_scope`` excludes a
    file one of its tasks declares must have that file admitted into
    ``phase.edit_scope`` after ``promote``, with no ``edit_scope_violation``.

    RED without the fix (Phase 1 edit_scope stays ``["index.js"]``).
    GREEN with it (Phase 1 edit_scope includes ``"test_index.js"``).
    """
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as fs_dir:
        fs_root = Path(fs_dir)
        artifact_dir = fs_root / ".autodev" / "tournaments" / "plan-scopefix"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "incumbent_after_01.md").write_text(
            _SALVAGE_NARROWED_SCOPE_MD, encoding="utf-8"
        )

        result = runner.invoke(
            cli,
            ["tournament", "promote", "--tournament-id", "plan-scopefix"],
        )
        assert result.exit_code == 0, (
            f"exit={result.exit_code}\noutput:\n{result.output}\nexc:{result.exception!r}"
        )

        plan_json_path = fs_root / ".autodev" / "plan.json"
        assert plan_json_path.exists(), "plan.json was not written"
        plan_data = json.loads(plan_json_path.read_text(encoding="utf-8"))

        # Locate Phase 1 in the persisted plan.
        phases = plan_data.get("phases") or plan_data.get("plan", {}).get("phases", [])
        assert phases, f"no phases in plan.json: {plan_data}"
        phase1 = phases[0]
        phase_scope = phase1.get("edit_scope")

        # The repair must have widened the scope to include the task-declared file.
        assert phase_scope is not None, (
            "Phase 1 edit_scope is None — repair did not materialise a scope"
        )
        assert "index.js" in phase_scope, (
            f"index.js missing from phase scope: {phase_scope}"
        )
        assert "test_index.js" in phase_scope, (
            f"F-1 bypass not fixed: test_index.js not admitted into phase scope "
            f"(got: {phase_scope})"
        )
