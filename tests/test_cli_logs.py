"""Tests for ``autodev logs`` CLI command.

v0.25.2 — replaces the prior "not yet implemented (Phase 4)" stub.
Operator surface for tailing per-session structured event streams.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from click.testing import CliRunner

from cli import cli
from config.defaults import default_config
from config.loader import save_config


def _write_config(cwd: Path) -> None:
    cfg = default_config()
    cfg.platform = "claude_code"  # type: ignore[assignment]
    autodev_dir = cwd / ".autodev"
    autodev_dir.mkdir(parents=True, exist_ok=True)
    save_config(cfg, autodev_dir / "config.json")


def _seed_session(cwd: Path, session_id: str, events: list[str]) -> Path:
    """Create ``.autodev/sessions/<sid>/events.jsonl`` containing one JSON
    line per entry in ``events``. Returns the file path."""
    sdir = cwd / ".autodev" / "sessions" / session_id
    sdir.mkdir(parents=True, exist_ok=True)
    fp = sdir / "events.jsonl"
    fp.write_text("\n".join(events) + "\n", encoding="utf-8")
    return fp


def _age_path(p: Path, seconds_ago: float) -> None:
    target = time.time() - seconds_ago
    os.utime(p, (target, target))


def test_logs_prints_session_events(tmp_path: Path) -> None:
    """``autodev logs --session SID`` cats the session's events.jsonl."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)
        _seed_session(
            cwd,
            "sess-aaa111222333",
            [
                '{"event":"hello","session_id":"sess-aaa111222333"}',
                '{"event":"world","session_id":"sess-aaa111222333"}',
            ],
        )

        result = runner.invoke(cli, ["logs", "--session", "sess-aaa111222333"])

        assert result.exit_code == 0, result.output
        assert "hello" in result.output
        assert "world" in result.output


def test_logs_no_sessions_clean_error(tmp_path: Path) -> None:
    """No sessions directory → exit 1 with a clear message, not a stack
    trace."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)

        result = runner.invoke(cli, ["logs"])

        assert result.exit_code == 1
        assert "no session" in result.output.lower()


def test_logs_finds_latest_by_mtime(tmp_path: Path) -> None:
    """With multiple sessions and no ``--session``, the newest mtime
    wins."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)
        old = _seed_session(
            cwd, "sess-old", ['{"event":"old_one","session_id":"sess-old"}']
        )
        new = _seed_session(
            cwd, "sess-new", ['{"event":"new_one","session_id":"sess-new"}']
        )
        _age_path(old, 86400)  # 1 day ago
        _age_path(new, 60)  # 1 minute ago

        result = runner.invoke(cli, ["logs"])

        assert result.exit_code == 0, result.output
        assert "new_one" in result.output
        assert "old_one" not in result.output


def test_logs_missing_session_id_errors(tmp_path: Path) -> None:
    """``--session sess-nonexistent`` when that session has no
    events.jsonl → exit 1 with the path-not-found message."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)
        # Create another session so sessions/ exists but the requested
        # one is missing.
        _seed_session(cwd, "sess-other", ['{"event":"x"}'])

        result = runner.invoke(cli, ["logs", "--session", "sess-missing"])

        assert result.exit_code == 1
        assert "sess-missing" in result.output


def test_logs_empty_events_file_succeeds(tmp_path: Path) -> None:
    """An empty events.jsonl is valid (newly-attached sink, no logs yet)
    and ``logs`` should exit 0 without complaint."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)
        sdir = cwd / ".autodev" / "sessions" / "sess-empty"
        sdir.mkdir(parents=True)
        (sdir / "events.jsonl").touch()

        result = runner.invoke(cli, ["logs", "--session", "sess-empty"])

        assert result.exit_code == 0, result.output
