"""Tests for ``autodev reset`` CLI command.

v0.25.2 — replaces the prior "not yet implemented (Phase 4)" stub. The
command clears plan state by default and, with ``--hard``, additionally
removes per-run artifacts (evidence, sessions, tournaments, etc.).
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from cli import cli
from config.defaults import default_config
from config.loader import save_config


def _write_config(cwd: Path) -> None:
    """Write a minimal valid config.json into <cwd>/.autodev/."""
    cfg = default_config()
    cfg.platform = "claude_code"  # type: ignore[assignment]
    autodev_dir = cwd / ".autodev"
    autodev_dir.mkdir(parents=True, exist_ok=True)
    save_config(cfg, autodev_dir / "config.json")


def _seed_full_state(cwd: Path) -> dict[str, Path]:
    """Populate ``.autodev/`` with one of every artifact reset/--hard touches
    plus all preserved artifacts. Returns a name -> path map for assertions."""
    root = cwd / ".autodev"
    root.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    # Reset-default scope (removed by `autodev reset` without --hard).
    paths["plan"] = root / "plan.json"
    paths["plan"].write_text('{"plan_id": "p"}', encoding="utf-8")
    paths["ledger"] = root / "plan-ledger.jsonl"
    paths["ledger"].write_text('{"op":"init_plan"}\n', encoding="utf-8")

    # --hard additional scope.
    paths["evidence"] = root / "evidence"
    paths["evidence"].mkdir()
    (paths["evidence"] / "1.1-developer.json").write_text("{}", encoding="utf-8")
    paths["delegations"] = root / "delegations"
    paths["delegations"].mkdir()
    (paths["delegations"] / "1.1-developer.md").write_text("d", encoding="utf-8")
    paths["responses"] = root / "responses"
    paths["responses"].mkdir()
    (paths["responses"] / "1.1-developer.json").write_text("{}", encoding="utf-8")
    paths["inline_state"] = root / "inline-state.json"
    paths["inline_state"].write_text("{}", encoding="utf-8")
    paths["tournaments"] = root / "tournaments"
    paths["tournaments"].mkdir()
    (paths["tournaments"] / "plan-abc").mkdir()
    paths["sessions"] = root / "sessions"
    paths["sessions"].mkdir()
    (paths["sessions"] / "sess-abc").mkdir()
    (paths["sessions"] / "sess-abc" / "events.jsonl").write_text(
        "{}", encoding="utf-8"
    )
    paths["debug"] = root / "debug"
    paths["debug"].mkdir()
    (paths["debug"] / "log.txt").write_text("dbg", encoding="utf-8")
    paths["lock"] = root / ".lock"
    paths["lock"].write_text("12345", encoding="utf-8")
    paths["worktrees"] = cwd / ".autodev" / "execute_worktrees"
    paths["worktrees"].mkdir()
    paths["worktrees_pool"] = cwd / ".autodev" / "execute_worktrees_pool"
    paths["worktrees_pool"].mkdir()

    # Always-preserved set.
    paths["config"] = root / "config.json"  # written by _write_config
    paths["spec"] = root / "spec.md"
    paths["spec"].write_text("# spec", encoding="utf-8")
    paths["secretscan_baseline"] = root / "secretscan-baseline.json"
    paths["secretscan_baseline"].write_text("{}", encoding="utf-8")
    paths["gitignore"] = root / ".gitignore"
    paths["gitignore"].write_text("*", encoding="utf-8")
    paths["knowledge"] = root / "knowledge.jsonl"
    paths["knowledge"].write_text('{"id":"k1"}\n', encoding="utf-8")
    paths["rejected_lessons"] = root / "rejected_lessons.jsonl"
    paths["rejected_lessons"].write_text('{"id":"r1"}\n', encoding="utf-8")
    paths["index_db"] = root / "index.db"
    paths["index_db"].write_text("sqlite", encoding="utf-8")
    paths["index_db_shm"] = root / "index.db-shm"
    paths["index_db_shm"].write_text("", encoding="utf-8")
    paths["index_db_wal"] = root / "index.db-wal"
    paths["index_db_wal"].write_text("", encoding="utf-8")
    paths["index_state"] = root / "index.state.json"
    paths["index_state"].write_text('{"sha":"deadbeef"}', encoding="utf-8")

    return paths


# ---------------------------------------------------------------------------
# autodev reset (default scope)
# ---------------------------------------------------------------------------


def test_reset_default_removes_plan_files(tmp_path: Path) -> None:
    """Default reset clears plan.json + plan-ledger.jsonl only."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)
        paths = _seed_full_state(cwd)

        result = runner.invoke(cli, ["reset"])

        assert result.exit_code == 0, result.output
        assert not paths["plan"].exists()
        assert not paths["ledger"].exists()


def test_reset_default_preserves_config_spec_index_knowledge(
    tmp_path: Path,
) -> None:
    """Default reset must NOT touch config, spec, knowledge, or the v0.25.0
    file index. Also must not touch evidence/sessions/etc. — those are
    --hard scope."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)
        paths = _seed_full_state(cwd)

        result = runner.invoke(cli, ["reset"])
        assert result.exit_code == 0, result.output

        # Always-preserved.
        assert paths["config"].exists()
        assert paths["spec"].exists()
        assert paths["secretscan_baseline"].exists()
        assert paths["gitignore"].exists()
        assert paths["knowledge"].exists()
        assert paths["rejected_lessons"].exists()
        assert paths["index_db"].exists()
        assert paths["index_state"].exists()

        # Not touched by default reset (only by --hard).
        assert paths["evidence"].exists()
        assert paths["sessions"].exists()
        assert paths["tournaments"].exists()
        assert paths["debug"].exists()
        assert paths["delegations"].exists()
        assert paths["responses"].exists()


# ---------------------------------------------------------------------------
# autodev reset --hard (extended scope)
# ---------------------------------------------------------------------------


def test_reset_hard_removes_evidence_sessions_tournaments(tmp_path: Path) -> None:
    """--hard additionally clears evidence/, sessions/, tournaments/,
    debug/, delegations/, responses/, inline-state.json, .lock, and the
    execute_worktrees pool dirs."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)
        paths = _seed_full_state(cwd)

        result = runner.invoke(cli, ["reset", "--hard"])

        assert result.exit_code == 0, result.output
        # Default scope also gone.
        assert not paths["plan"].exists()
        assert not paths["ledger"].exists()
        # --hard scope.
        assert not paths["evidence"].exists()
        assert not paths["delegations"].exists()
        assert not paths["responses"].exists()
        assert not paths["inline_state"].exists()
        assert not paths["tournaments"].exists()
        assert not paths["sessions"].exists()
        assert not paths["debug"].exists()
        assert not paths["lock"].exists()
        assert not paths["worktrees"].exists()
        assert not paths["worktrees_pool"].exists()


def test_reset_hard_preserves_config_spec_index_knowledge(tmp_path: Path) -> None:
    """--hard must still preserve config, spec, baselines, knowledge, and
    the file index — operator should never lose those without explicit
    workspace-wipe."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)
        paths = _seed_full_state(cwd)

        result = runner.invoke(cli, ["reset", "--hard"])
        assert result.exit_code == 0, result.output

        assert paths["config"].exists()
        assert paths["spec"].exists()
        assert paths["secretscan_baseline"].exists()
        assert paths["gitignore"].exists()
        assert paths["knowledge"].exists()
        assert paths["rejected_lessons"].exists()
        assert paths["index_db"].exists()
        assert paths["index_db_shm"].exists()
        assert paths["index_db_wal"].exists()
        assert paths["index_state"].exists()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_reset_idempotent_on_empty_dir(tmp_path: Path) -> None:
    """Reset on a workspace with no plan-state must succeed and emit a
    'nothing to reset' message, not error."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)
        # No state beyond config.json — nothing to reset.

        result = runner.invoke(cli, ["reset"])

        assert result.exit_code == 0, result.output
        assert "nothing to reset" in result.output.lower()


def test_reset_emits_removed_paths_summary(tmp_path: Path) -> None:
    """Reset output names the paths it removed so the operator can audit."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)
        _seed_full_state(cwd)

        result = runner.invoke(cli, ["reset", "--hard"])

        assert result.exit_code == 0, result.output
        # At minimum, mention plan + ledger + evidence + sessions.
        out = result.output
        assert "plan.json" in out
        assert "plan-ledger.jsonl" in out
        assert "evidence" in out
        assert "sessions" in out
