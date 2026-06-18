"""Tests for v0.25.0 ``autodev execute`` per-trigger incremental index hook.

The hook in ``src/cli/commands/execute.py:_maybe_refresh_index`` runs
*before* ``Orchestrator(...)`` instantiation. It:

  1. Skips silently when ``cfg.index_enabled=False``.
  2. Skips silently when ``.autodev/index.db.building`` marker exists
     (an async build is in progress; race-free wait).
  3. Builds full when ``.autodev/index.db`` is missing.
  4. Otherwise runs ``build_incremental`` keyed off the persisted
     ``last_indexed_sha`` (read via ``_last_indexed_sha`` helper).

These tests mock the index module to verify the dispatch logic without
depending on the parallel agent's core code.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from cli import cli
from config.defaults import default_config
from config.loader import save_config


def _write_config(cwd: Path) -> None:
    """Bootstrap a minimal valid config.json at ``<cwd>/.autodev/config.json``."""
    cfg = default_config()
    cfg.platform = "claude_code"
    autodev_dir = cwd / ".autodev"
    autodev_dir.mkdir(parents=True, exist_ok=True)
    save_config(cfg, autodev_dir / "config.json")


def test_execute_runs_incremental_when_index_exists(tmp_path: Path) -> None:
    """When ``.autodev/index.db`` exists and no building marker is present,
    the hook calls ``IndexBuilder.build_incremental`` (NOT build_full)."""
    runner = CliRunner()

    fake_index_module = mock.MagicMock()
    fake_index_module.IndexBuilder.build_full = mock.MagicMock()
    fake_index_module.IndexBuilder.build_incremental = mock.MagicMock()
    fake_index_module._last_indexed_sha = mock.MagicMock(return_value="abc123")

    with runner.isolated_filesystem(temp_dir=tmp_path) as cwd:
        cwd_path = Path(cwd)
        _write_config(cwd_path)
        # Pre-create the db file to simulate an existing index.
        (cwd_path / ".autodev" / "index.db").write_bytes(b"fake")

        with mock.patch.dict(
            "sys.modules", {"state.file_index": fake_index_module}
        ):
            # The execute command will exit early (no plan) — we just need
            # to verify the index hook fired before Orchestrator construction.
            # Side-effecting call: triggers the index hook the test verifies.
            runner.invoke(cli, ["execute"], catch_exceptions=True)

        # build_incremental should be called once; build_full NOT called.
        assert fake_index_module.IndexBuilder.build_incremental.call_count == 1
        assert fake_index_module.IndexBuilder.build_full.call_count == 0
        # build_incremental should have been called with the sha read from
        # ``_last_indexed_sha``.
        kwargs = fake_index_module.IndexBuilder.build_incremental.call_args.kwargs
        assert kwargs.get("since_sha") == "abc123"


def test_execute_runs_full_when_index_missing(tmp_path: Path) -> None:
    """When ``.autodev/index.db`` is absent (and no building marker), the
    hook falls back to ``IndexBuilder.build_full``."""
    runner = CliRunner()

    fake_index_module = mock.MagicMock()
    fake_index_module.IndexBuilder.build_full = mock.MagicMock()
    fake_index_module.IndexBuilder.build_incremental = mock.MagicMock()
    fake_index_module._last_indexed_sha = mock.MagicMock()

    with runner.isolated_filesystem(temp_dir=tmp_path) as cwd:
        cwd_path = Path(cwd)
        _write_config(cwd_path)
        # No db file — the hook should hit build_full.

        with mock.patch.dict(
            "sys.modules", {"state.file_index": fake_index_module}
        ):
            # Side-effecting call: triggers the index hook the test verifies.
            runner.invoke(cli, ["execute"], catch_exceptions=True)

        assert fake_index_module.IndexBuilder.build_full.call_count == 1
        assert fake_index_module.IndexBuilder.build_incremental.call_count == 0


def test_execute_skips_when_marker_exists(tmp_path: Path) -> None:
    """When ``.autodev/index.db.building`` marker exists, the hook short-circuits
    (an async build is in flight). Neither build_full nor build_incremental
    fires, even if the db file itself exists."""
    runner = CliRunner()

    fake_index_module = mock.MagicMock()
    fake_index_module.IndexBuilder.build_full = mock.MagicMock()
    fake_index_module.IndexBuilder.build_incremental = mock.MagicMock()

    with runner.isolated_filesystem(temp_dir=tmp_path) as cwd:
        cwd_path = Path(cwd)
        _write_config(cwd_path)
        autodev = cwd_path / ".autodev"
        # Both the building marker AND the db file present — marker wins.
        (autodev / "index.db").write_bytes(b"fake")
        (autodev / "index.db.building").write_text("pid=1234")

        with mock.patch.dict(
            "sys.modules", {"state.file_index": fake_index_module}
        ):
            # Side-effecting call: triggers the index hook the test verifies.
            runner.invoke(cli, ["execute"], catch_exceptions=True)

        assert fake_index_module.IndexBuilder.build_full.call_count == 0
        assert fake_index_module.IndexBuilder.build_incremental.call_count == 0
