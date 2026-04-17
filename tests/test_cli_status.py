"""Tests for ``autodev status`` CLI command."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner
from rich.console import Console

from cli import cli
from cli.commands.status import _print_knowledge_summary
from config.defaults import default_config
from config.loader import save_config
from errors import AutodevError
from state.schemas import Phase, Plan, Task


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


def _make_plan_with_tasks() -> Plan:
    """Return a Plan with tasks in various states."""
    return Plan(
        plan_id="plan-status-test",
        spec_hash="abc",
        phases=[
            Phase(
                id="1",
                title="Phase 1",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="Done task",
                        description="Already done",
                        status="complete",
                        retry_count=0,
                    ),
                    Task(
                        id="1.2",
                        phase_id="1",
                        title="Blocked task",
                        description="Blocked by dependency",
                        status="blocked",
                        retry_count=2,
                    ),
                ],
            ),
            Phase(
                id="2",
                title="Phase 2",
                tasks=[
                    Task(
                        id="2.1",
                        phase_id="2",
                        title="Pending task",
                        description="Not started",
                        status="pending",
                        retry_count=0,
                    ),
                    Task(
                        id="2.2",
                        phase_id="2",
                        title="Skipped task",
                        description="Skipped for now",
                        status="skipped",
                        retry_count=1,
                    ),
                ],
            ),
        ],
        metadata={"title": "Status Test Plan"},
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_status_missing_config_exits_1(tmp_path: Path) -> None:
    """Status in a directory without .autodev/config.json should exit 1."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(cli, ["status"])
    assert result.exit_code == 1
    assert "autodev init" in result.output


def test_status_config_error_exits_1(tmp_path: Path) -> None:
    """Status with invalid JSON config should exit 1."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        autodev_dir = cwd / ".autodev"
        autodev_dir.mkdir(parents=True, exist_ok=True)
        (autodev_dir / "config.json").write_text("not valid json", encoding="utf-8")

        result = runner.invoke(cli, ["status"])
    assert result.exit_code == 1
    assert "config error" in result.output


def test_status_no_plan_shows_message(tmp_path: Path) -> None:
    """Status with valid config but no plan shows 'No plan yet' message."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)

        with (
            patch("cli.commands.status.PlanManager") as mock_pm_cls,
            patch("cli.commands.status.KnowledgeStore") as mock_ks_cls,
        ):
            mock_pm = MagicMock()
            mock_pm.load = AsyncMock(return_value=None)
            mock_pm_cls.return_value = mock_pm

            mock_ks = MagicMock()
            mock_ks.read_all = AsyncMock(return_value=[])
            mock_ks.hive_enabled = False
            mock_ks_cls.return_value = mock_ks

            result = runner.invoke(cli, ["status"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert "No plan yet" in result.output


def test_status_with_plan_renders_table(tmp_path: Path) -> None:
    """Status with a plan shows a table of tasks with their statuses."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)

        plan = _make_plan_with_tasks()

        with (
            patch("cli.commands.status.PlanManager") as mock_pm_cls,
            patch("cli.commands.status.KnowledgeStore") as mock_ks_cls,
            patch("cli.commands.status.list_evidence") as mock_list_ev,
        ):
            mock_pm = MagicMock()
            mock_pm.load = AsyncMock(return_value=plan)
            mock_pm_cls.return_value = mock_pm

            mock_ks = MagicMock()
            mock_ks.read_all = AsyncMock(return_value=[])
            mock_ks.hive_enabled = False
            mock_ks_cls.return_value = mock_ks

            mock_list_ev.return_value = []

            result = runner.invoke(cli, ["status"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    # Verify task statuses appear
    assert "complete" in result.output
    assert "blocked" in result.output
    assert "pending" in result.output
    assert "skipped" in result.output
    # Verify plan title appears
    assert "Status Test Plan" in result.output


def test_status_autodev_error_exits_2(tmp_path: Path) -> None:
    """Status exits 2 when an AutodevError is raised during execution."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)

        with patch("cli.commands.status.PlanManager") as mock_pm_cls:
            mock_pm = MagicMock()
            mock_pm.load = AsyncMock(
                side_effect=AutodevError("ledger corrupted")
            )
            mock_pm_cls.return_value = mock_pm

            result = runner.invoke(cli, ["status"])

    assert result.exit_code == 2
    assert "status failed" in result.output


def test_print_knowledge_summary() -> None:
    """_print_knowledge_summary renders swarm and hive counts."""
    output = StringIO()
    console = Console(file=output, force_terminal=False)

    _print_knowledge_summary(console, swarm_count=5, hive_count=3)

    rendered = output.getvalue()
    assert "5" in rendered
    assert "3" in rendered
    assert "swarm" in rendered.lower() or "swarm" in rendered
    assert "hive" in rendered.lower() or "hive" in rendered


def test_print_knowledge_summary_zero_counts() -> None:
    """_print_knowledge_summary handles zero counts gracefully."""
    output = StringIO()
    console = Console(file=output, force_terminal=False)

    _print_knowledge_summary(console, swarm_count=0, hive_count=0)

    rendered = output.getvalue()
    assert "0" in rendered
