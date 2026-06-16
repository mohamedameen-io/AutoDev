"""Tests for v0.31.0 (Phase 5.6) doctor extensions."""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from cli.commands.doctor import doctor
from config.defaults import default_config
from config.loader import save_config
from plugins.registry import PluginRegistry


def _seed_workspace(cwd: Path) -> None:
    """Create a minimal .autodev/config.json in ``cwd``."""
    (cwd / ".autodev").mkdir(parents=True, exist_ok=True)
    save_config(default_config(), cwd / ".autodev" / "config.json")


def _invoke_doctor(extra_args: list[str] | None = None):
    """Run doctor with discover_plugins patched out."""
    runner = CliRunner()
    args = list(extra_args or [])
    with patch(
        "cli.commands.doctor.discover_plugins",
        return_value=PluginRegistry(),
    ):
        return runner.invoke(doctor, args, catch_exceptions=False)


def test_shows_language_profile(tmp_path: Path) -> None:
    """Doctor renders the codebase language profile section with shares."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _seed_workspace(cwd)
        # Seed a clearly TS-heavy repo.
        (cwd / "src").mkdir()
        for i in range(3):
            (cwd / "src" / f"app{i}.ts").write_text("export {};\n", encoding="utf-8")

        result = _invoke_doctor()

    out = result.output
    # Rich may wrap the title across lines; check both halves.
    assert "Codebase language" in out
    assert "profile" in out
    assert "typescript" in out
    # Shares are formatted as percentages.
    assert "%" in out


def test_shows_adapter_fitness_warning(tmp_path: Path) -> None:
    """Doctor warns when the configured adapter scores below threshold."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        # Force cursor as the platform but stage a Python-heavy repo
        # (cursor scores 30 on no-TS-or-JS codebases -> warning).
        (cwd / ".autodev").mkdir(parents=True, exist_ok=True)
        cfg = default_config()
        cfg.platform = "cursor"  # type: ignore[assignment]
        save_config(cfg, cwd / ".autodev" / "config.json")
        for i in range(5):
            (cwd / f"mod_{i}.py").write_text("x = 1\n", encoding="utf-8")

        result = _invoke_doctor()

    out = result.output
    assert "Adapter fitness" in out
    assert "30/100" in out
    # Warning text is surfaced.
    assert "Consider switching adapters" in out


def test_shows_orphan_worktree_count(tmp_path: Path) -> None:
    """Doctor reports orphan-worktree counts from the manifest + disk scan."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _seed_workspace(cwd)
        # Stage an on-disk-only orphan: a directory under
        # execute_worktrees/ with no manifest entry.
        ew = cwd / ".autodev" / "execute_worktrees"
        ew.mkdir(parents=True)
        (ew / "ghost-tournament").mkdir()

        result = _invoke_doctor()

    out = result.output
    assert "Orphan worktrees" in out
    assert "on_disk_not_in_manifest" in out
    # We staged exactly one orphan, so the row should show "1".
    assert " 1" in out  # leading space because rich right-aligns


def test_shows_stale_editor_agent_files_warning(tmp_path: Path) -> None:
    """Doctor warns when .claude/agents/ is older than .autodev/config.json."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _seed_workspace(cwd)
        # Stage a stale Claude agents dir.
        agents = cwd / ".claude" / "agents"
        agents.mkdir(parents=True)
        agent_file = agents / "developer.md"
        agent_file.write_text("# stale\n", encoding="utf-8")
        # Make the agent file *older* than the config (1h in the past).
        past = time.time() - 3600
        os.utime(agent_file, (past, past))
        os.utime(agents, (past, past))

        # Bump config mtime forward so it's clearly newer.
        cfg_path = cwd / ".autodev" / "config.json"
        future = time.time() + 60
        os.utime(cfg_path, (future, future))

        result = _invoke_doctor()

    out = result.output
    assert "Editor agent files" in out
    assert "STALE" in out
    assert "Stale editor agent files detected" in out


def test_repair_worktrees_lists_only(tmp_path: Path) -> None:
    """``--repair-worktrees`` lists orphan paths but never deletes."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _seed_workspace(cwd)
        ew = cwd / ".autodev" / "execute_worktrees"
        ew.mkdir(parents=True)
        ghost = ew / "ghost-tournament"
        ghost.mkdir()

        result = _invoke_doctor(["--repair-worktrees"])

    out = result.output
    assert "Orphan paths (NOT deleted)" in out
    assert "ghost-tournament" in out
    # Path must still exist on disk.
    assert ghost.exists()
