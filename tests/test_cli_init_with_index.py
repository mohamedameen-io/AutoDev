"""Tests for v0.25.0 ``autodev init`` index build hook.

The hook lives in ``src/cli/commands/init.py`` and either:

  * synchronously calls ``IndexBuilder.build_full`` when the repo is
    small/medium (``RepoCapacity.is_huge=False``), OR
  * spawns a background ``subprocess.Popen([sys.executable, "-m",
    "state.file_index", ...])`` when the repo is huge AND
    ``cfg.index_huge_repo_async_init=True``.

These tests mock both ``probe_repo`` and ``IndexBuilder``/``subprocess.Popen``
so they exercise the wiring without depending on the parallel agent's
``state.file_index`` core landing first.

Three behaviors covered (per the v0.25.0 plan):

  * ``test_init_builds_empty_index_for_empty_repo`` — synchronous build
    runs for a small (non-huge) repo with no source files.
  * ``test_init_builds_populated_index_for_small_fixture`` — synchronous
    build runs for a small repo with a few source files; ``IndexBuilder``
    is invoked exactly once.
  * ``test_init_async_for_huge_repo`` — ``RepoCapacity.is_huge=True``
    and ``cfg.index_huge_repo_async_init=True`` triggers the background
    ``subprocess.Popen`` spawn instead of synchronous build.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from cli import cli


class _FakeStats:
    """Stand-in for ``state.file_index.IndexStats``."""

    def __init__(self, file_count: int, symbol_count: int) -> None:
        self.file_count = file_count
        self.symbol_count = symbol_count
        self.duration_ms = 12
        self.full_rebuild = True


class _FakeCapacity:
    """Stand-in for ``runtime.repo_probe.RepoCapacity``."""

    def __init__(self, *, is_huge: bool) -> None:
        self.is_huge = is_huge
        self.file_count = 99999 if is_huge else 5
        self.total_bytes = 0
        self.depth_max = 1
        self.avg_file_size_bytes = 0
        self.largest_dir = ""
        self.largest_dir_file_count = 0


def test_init_builds_empty_index_for_empty_repo(tmp_path: Path) -> None:
    """Synchronous build path runs on small/empty repos. ``IndexBuilder.build_full``
    is invoked once with the cwd + db path; subprocess.Popen is NOT used."""
    runner = CliRunner()

    fake_index_module = mock.MagicMock()
    fake_index_module.IndexBuilder.build_full = mock.MagicMock(
        return_value=_FakeStats(0, 0)
    )

    fake_probe_module = mock.MagicMock()
    fake_probe_module.probe_repo = mock.MagicMock(
        return_value=_FakeCapacity(is_huge=False)
    )

    with runner.isolated_filesystem(temp_dir=tmp_path) as cwd:
        with mock.patch.dict(
            "sys.modules",
            {
                "state.file_index": fake_index_module,
            },
        ), mock.patch(
            "runtime.repo_probe.probe_repo",
            fake_probe_module.probe_repo,
        ), mock.patch(
            "subprocess.Popen"
        ) as mock_popen:
            result = runner.invoke(cli, ["init"], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        # Synchronous build called once; subprocess.Popen not called.
        assert fake_index_module.IndexBuilder.build_full.call_count == 1
        assert mock_popen.call_count == 0


def test_init_builds_populated_index_for_small_fixture(tmp_path: Path) -> None:
    """Same as the empty-repo case but with a non-zero stat — verifies the
    summary line emits the file/symbol counts."""
    runner = CliRunner()

    fake_index_module = mock.MagicMock()
    fake_index_module.IndexBuilder.build_full = mock.MagicMock(
        return_value=_FakeStats(file_count=42, symbol_count=137)
    )

    fake_probe_module = mock.MagicMock()
    fake_probe_module.probe_repo = mock.MagicMock(
        return_value=_FakeCapacity(is_huge=False)
    )

    with runner.isolated_filesystem(temp_dir=tmp_path) as cwd:
        with mock.patch.dict(
            "sys.modules", {"state.file_index": fake_index_module}
        ), mock.patch(
            "runtime.repo_probe.probe_repo",
            fake_probe_module.probe_repo,
        ), mock.patch(
            "subprocess.Popen"
        ) as mock_popen:
            result = runner.invoke(cli, ["init"], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        assert fake_index_module.IndexBuilder.build_full.call_count == 1
        assert mock_popen.call_count == 0
        # Console summary should mention the file/symbol counts.
        assert "42 files" in result.output
        assert "137 symbols" in result.output


def test_init_async_for_huge_repo(tmp_path: Path) -> None:
    """``RepoCapacity.is_huge=True`` triggers the async subprocess spawn path
    instead of synchronous build. ``IndexBuilder.build_full`` must NOT be
    called in-process; ``subprocess.Popen`` IS called with the expected
    ``state.file_index`` module CLI."""
    runner = CliRunner()

    fake_index_module = mock.MagicMock()
    fake_index_module.IndexBuilder.build_full = mock.MagicMock(
        return_value=_FakeStats(0, 0)
    )

    fake_probe_module = mock.MagicMock()
    fake_probe_module.probe_repo = mock.MagicMock(
        return_value=_FakeCapacity(is_huge=True)
    )

    # The async escape hatch is opt-in (default flipped to sync in the
    # parallel-build work). Enable it explicitly to exercise the spawn path.
    from config.defaults import default_config as _real_default_config

    def _async_enabled_config():
        cfg = _real_default_config()
        cfg.index_huge_repo_async_init = True
        return cfg

    with runner.isolated_filesystem(temp_dir=tmp_path) as cwd:
        with mock.patch.dict(
            "sys.modules", {"state.file_index": fake_index_module}
        ), mock.patch(
            "runtime.repo_probe.probe_repo",
            fake_probe_module.probe_repo,
        ), mock.patch(
            "cli.commands.init.default_config",
            _async_enabled_config,
        ), mock.patch(
            "subprocess.Popen"
        ) as mock_popen:
            result = runner.invoke(cli, ["init"], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        # Synchronous build NOT called; subprocess.Popen IS called.
        assert fake_index_module.IndexBuilder.build_full.call_count == 0
        assert mock_popen.call_count == 1
        # Verify the spawned command targets the file-index module.
        spawn_args = mock_popen.call_args[0][0]
        assert spawn_args[0] == sys.executable
        assert spawn_args[1] == "-m"
        assert spawn_args[2] == "state.file_index"
        assert spawn_args[3] == "build-full"
        # Output should mention the background build.
        assert "background" in result.output.lower() or "running" in result.output.lower()
