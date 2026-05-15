"""Tests for ``autodev prune`` CLI command.

v0.25.2 — replaces the prior "not yet implemented (Phase 10)" stub.
Garbage-collects per-run artifacts older than the configured threshold:
tournament subdirs, session subdirs, and individual evidence files.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli import cli
from cli.commands.prune import _parse_duration
from config.defaults import default_config
from config.loader import save_config


def _write_config(cwd: Path) -> None:
    cfg = default_config()
    cfg.platform = "claude_code"  # type: ignore[assignment]
    autodev_dir = cwd / ".autodev"
    autodev_dir.mkdir(parents=True, exist_ok=True)
    save_config(cfg, autodev_dir / "config.json")


def _age_path(p: Path, seconds_ago: float) -> None:
    """Backdate the mtime of a file or directory to ``seconds_ago``."""
    target = time.time() - seconds_ago
    os.utime(p, (target, target))


# ---------------------------------------------------------------------------
# _parse_duration helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected_seconds",
    [
        ("30d", 30 * 86400.0),
        ("7d", 7 * 86400.0),
        ("24h", 24 * 3600.0),
        ("1h", 3600.0),
        ("60m", 3600.0),
        ("30s", 30.0),
    ],
)
def test_parse_duration_accepts_supported_units(
    text: str, expected_seconds: float
) -> None:
    assert _parse_duration(text) == expected_seconds


@pytest.mark.parametrize("bad", ["", "30", "30x", "abc", "-5d", "30 d"])
def test_parse_duration_rejects_invalid(bad: str) -> None:
    with pytest.raises(ValueError):
        _parse_duration(bad)


# ---------------------------------------------------------------------------
# Prune behavior
# ---------------------------------------------------------------------------


def test_prune_invalid_duration_exits_1(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)

        result = runner.invoke(cli, ["prune", "--older-than", "bogus"])

        assert result.exit_code == 1
        assert "invalid" in result.output.lower() or "duration" in result.output.lower()


def test_prune_removes_old_tournaments_keeps_recent(tmp_path: Path) -> None:
    """Tournament subdir aged 60d gets removed; one aged 1d survives."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)

        t_dir = cwd / ".autodev" / "tournaments"
        t_dir.mkdir(parents=True)
        old = t_dir / "plan-old"
        old.mkdir()
        (old / "history.json").write_text("{}", encoding="utf-8")
        new = t_dir / "plan-new"
        new.mkdir()
        (new / "history.json").write_text("{}", encoding="utf-8")
        _age_path(old, 60 * 86400)
        _age_path(new, 1 * 86400)

        result = runner.invoke(cli, ["prune", "--older-than", "30d"])

        assert result.exit_code == 0, result.output
        assert not old.exists()
        assert new.exists()


def test_prune_removes_old_sessions_keeps_recent(tmp_path: Path) -> None:
    """Session subdir aged 60d gets removed; one aged 1d survives."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)

        s_dir = cwd / ".autodev" / "sessions"
        s_dir.mkdir(parents=True)
        old = s_dir / "sess-old"
        old.mkdir()
        (old / "events.jsonl").write_text("{}\n", encoding="utf-8")
        new = s_dir / "sess-new"
        new.mkdir()
        (new / "events.jsonl").write_text("{}\n", encoding="utf-8")
        _age_path(old, 60 * 86400)
        _age_path(new, 1 * 86400)

        result = runner.invoke(cli, ["prune", "--older-than", "30d"])

        assert result.exit_code == 0, result.output
        assert not old.exists()
        assert new.exists()


def test_executor_only_all_removes_fresh_worktrees(tmp_path: Path) -> None:
    """``--executor-only --all`` sweeps fresh worktree dirs for emergency cleanup."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)

        ew = cwd / ".autodev" / "execute_worktrees"
        ew.mkdir(parents=True)
        fresh = ew / "tasks" / "1.1"
        fresh.mkdir(parents=True)
        (fresh / "scratch.txt").write_text("x", encoding="utf-8")

        ewp = cwd / ".autodev" / "execute_worktrees_pool"
        ewp.mkdir(parents=True)
        pooled = ewp / "pool-0"
        pooled.mkdir()
        # No backdating — these are fresh.

        result = runner.invoke(
            cli, ["prune", "--executor-only", "--all"]
        )
        assert result.exit_code == 0, result.output
        # Children removed:
        assert not (ew / "tasks").exists()
        assert not pooled.exists()


def test_executor_only_skips_tournaments(tmp_path: Path) -> None:
    """``--executor-only`` ignores tournaments/sessions/evidence."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)

        # Stale tournament (would be swept by default mode).
        t_dir = cwd / ".autodev" / "tournaments"
        t_dir.mkdir(parents=True)
        old_t = t_dir / "plan-old"
        old_t.mkdir()
        _age_path(old_t, 60 * 86400)

        # Stale executor worktree (immediate child must be aged).
        ew = cwd / ".autodev" / "execute_worktrees"
        ew.mkdir(parents=True)
        wt_old = ew / "tournament-old"
        wt_old.mkdir()
        _age_path(wt_old, 60 * 86400)

        result = runner.invoke(
            cli, ["prune", "--executor-only", "--older-than", "30d"]
        )
        assert result.exit_code == 0, result.output
        # Tournament untouched, executor children pruned.
        assert old_t.exists()
        assert not wt_old.exists()


def test_default_prune_does_not_touch_executor_worktrees(tmp_path: Path) -> None:
    """Default prune ignores executor worktrees even when stale."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)

        ew = cwd / ".autodev" / "execute_worktrees"
        ew.mkdir(parents=True)
        wt_old = ew / "tournament-old"
        wt_old.mkdir()
        _age_path(wt_old, 60 * 86400)

        ewp = cwd / ".autodev" / "execute_worktrees_pool"
        ewp.mkdir(parents=True)
        pool_old = ewp / "pool-0"
        pool_old.mkdir()
        _age_path(pool_old, 60 * 86400)

        result = runner.invoke(cli, ["prune", "--older-than", "30d"])
        assert result.exit_code == 0, result.output
        assert wt_old.exists(), "default prune must NOT touch executor worktrees"
        assert pool_old.exists(), "default prune must NOT touch executor pool"


def test_prune_dry_run_lists_without_deleting(tmp_path: Path) -> None:
    """``--dry-run`` reports what WOULD be pruned, removes nothing."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)

        t_dir = cwd / ".autodev" / "tournaments"
        t_dir.mkdir(parents=True)
        old = t_dir / "plan-stale"
        old.mkdir()
        _age_path(old, 60 * 86400)

        result = runner.invoke(
            cli, ["prune", "--older-than", "30d", "--dry-run"]
        )

        assert result.exit_code == 0, result.output
        assert old.exists(), "dry-run must not delete anything"
        assert "plan-stale" in result.output
