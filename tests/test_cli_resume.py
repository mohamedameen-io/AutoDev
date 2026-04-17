"""Tests for ``autodev resume`` CLI command."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner
from rich.console import Console

from cli import cli
from cli.commands.resume import _render_resume_summary
from config.defaults import default_config
from config.loader import save_config
from errors import AutodevError
from state.schemas import Task


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(cwd: Path, platform: str = "claude_code") -> None:
    """Write a minimal valid config.json into <cwd>/.autodev/."""
    cfg = default_config()
    cfg.platform = platform  # type: ignore[assignment]
    autodev_dir = cwd / ".autodev"
    autodev_dir.mkdir(parents=True, exist_ok=True)
    save_config(cfg, autodev_dir / "config.json")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_resume_missing_config_exits_1(tmp_path: Path) -> None:
    """Resume in a directory without .autodev/config.json should exit 1."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(cli, ["resume"])
    assert result.exit_code == 1
    assert "autodev init" in result.output


def test_resume_config_error_exits_1(tmp_path: Path) -> None:
    """Resume with invalid JSON config should exit 1."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        autodev_dir = cwd / ".autodev"
        autodev_dir.mkdir(parents=True, exist_ok=True)
        (autodev_dir / "config.json").write_text("}{bad", encoding="utf-8")

        result = runner.invoke(cli, ["resume"])
    assert result.exit_code == 1
    assert "config error" in result.output


def test_resume_autodev_error_exits_2(tmp_path: Path) -> None:
    """Resume exits 2 when orchestrator raises AutodevError."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)

        with (
            patch("orchestrator.inline_state.load_suspend_state", return_value=None),
            patch("cli.commands.resume.get_adapter") as mock_get_adapter,
            patch("cli.commands.resume.Orchestrator") as mock_orch_cls,
        ):
            mock_adapter = MagicMock()
            mock_get_adapter.return_value = mock_adapter

            mock_orch = MagicMock()
            mock_orch.resume = AsyncMock(
                side_effect=AutodevError("resume failed: no plan")
            )
            mock_orch_cls.return_value = mock_orch

            result = runner.invoke(cli, ["resume"])

    assert result.exit_code == 2
    assert "resume failed" in result.output


def test_resume_success_no_suspend_state(tmp_path: Path) -> None:
    """Resume with no inline state calls get_adapter and orchestrator.resume()."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)

        tasks = [
            Task(
                id="1.1",
                phase_id="1",
                title="Resumed task",
                description="Resumed OK",
                status="complete",
                retry_count=1,
            ),
        ]

        with (
            patch("orchestrator.inline_state.load_suspend_state", return_value=None),
            patch("cli.commands.resume.get_adapter") as mock_get_adapter,
            patch("cli.commands.resume.Orchestrator") as mock_orch_cls,
        ):
            mock_adapter = MagicMock()
            mock_get_adapter.return_value = mock_adapter

            mock_orch = MagicMock()
            mock_orch.resume = AsyncMock(return_value=tasks)
            mock_orch_cls.return_value = mock_orch

            result = runner.invoke(cli, ["resume"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert "1.1" in result.output


# ---------------------------------------------------------------------------
# Direct _render_resume_summary tests
# ---------------------------------------------------------------------------


def test_render_resume_summary_no_tasks() -> None:
    """_render_resume_summary with empty list shows nothing-to-resume message."""
    output = StringIO()
    console = Console(file=output, force_terminal=False)

    _render_resume_summary(console, [])

    rendered = output.getvalue()
    assert "Nothing to resume" in rendered


def test_render_resume_summary_with_tasks() -> None:
    """_render_resume_summary renders tasks with correct statuses and retry counts."""
    output = StringIO()
    console = Console(file=output, force_terminal=False)

    tasks = [
        Task(
            id="1.1",
            phase_id="1",
            title="First",
            description="desc",
            status="complete",
            retry_count=0,
        ),
        Task(
            id="1.2",
            phase_id="1",
            title="Second",
            description="desc",
            status="blocked",
            retry_count=2,
        ),
        Task(
            id="2.1",
            phase_id="2",
            title="Third",
            description="desc",
            status="in_progress",
            retry_count=1,
        ),
    ]

    _render_resume_summary(console, tasks)

    rendered = output.getvalue()
    assert "Resumed" in rendered
    assert "3 tasks" in rendered
    assert "1.1" in rendered
    assert "1.2" in rendered
    assert "2.1" in rendered
    assert "complete" in rendered
    assert "blocked" in rendered
    assert "in_progress" in rendered


def test_render_resume_summary_single_task_with_retries() -> None:
    """_render_resume_summary shows retry count for a single retried task."""
    output = StringIO()
    console = Console(file=output, force_terminal=False)

    tasks = [
        Task(
            id="3.1",
            phase_id="3",
            title="Retried",
            description="desc",
            status="complete",
            retry_count=5,
        ),
    ]

    _render_resume_summary(console, tasks)

    rendered = output.getvalue()
    assert "3.1" in rendered
    assert "5" in rendered  # retry count
