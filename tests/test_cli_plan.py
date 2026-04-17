"""Tests for ``autodev plan`` CLI command."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from cli import cli
from cli.commands.plan import _render_plan_summary
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


def _make_plan() -> Plan:
    """Return a minimal Plan object for testing."""
    return Plan(
        plan_id="plan-001",
        spec_hash="abc123",
        phases=[
            Phase(
                id="1",
                title="Setup",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="Create module",
                        description="Create the main module",
                        files=["src/main.py"],
                    ),
                    Task(
                        id="1.2",
                        phase_id="1",
                        title="Add tests",
                        description="Add unit tests",
                        files=[],
                    ),
                ],
            ),
            Phase(
                id="2",
                title="Integration",
                tasks=[
                    Task(
                        id="2.1",
                        phase_id="2",
                        title="Wire up API",
                        description="Connect endpoints",
                        files=["src/api.py", "src/routes.py"],
                    ),
                ],
            ),
        ],
        metadata={"title": "Test Plan"},
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_plan_missing_config_exits_1(tmp_path: Path) -> None:
    """Plan in a directory without .autodev/config.json should exit 1."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(cli, ["plan", "build a widget"])
    assert result.exit_code == 1
    assert "autodev init" in result.output


def test_plan_invalid_config_exits_1(tmp_path: Path) -> None:
    """Plan with invalid JSON config should exit 1."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        autodev_dir = cwd / ".autodev"
        autodev_dir.mkdir(parents=True, exist_ok=True)
        (autodev_dir / "config.json").write_text("{invalid json!!}", encoding="utf-8")

        result = runner.invoke(cli, ["plan", "build a widget"])
    assert result.exit_code == 1
    assert "config error" in result.output


def test_plan_success_renders_output(tmp_path: Path) -> None:
    """Plan succeeds when orchestrator returns a valid Plan object."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)

        mock_plan = _make_plan()

        with (
            patch("cli.commands.plan.get_adapter") as mock_get_adapter,
            patch("cli.commands.plan.Orchestrator") as mock_orch_cls,
        ):
            mock_adapter = MagicMock()
            mock_get_adapter.return_value = mock_adapter

            mock_orch = MagicMock()
            mock_orch.plan = AsyncMock(return_value=mock_plan)
            mock_orch_cls.return_value = mock_orch

            result = runner.invoke(
                cli, ["plan", "build a widget"], catch_exceptions=False
            )

    assert result.exit_code == 0, result.output
    assert "Plan approved" in result.output or "Test Plan" in result.output
    assert "plan-001" in result.output


def test_plan_autodev_error_exits_2(tmp_path: Path) -> None:
    """Plan exits 2 when orchestrator raises AutodevError."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)

        with (
            patch("cli.commands.plan.get_adapter") as mock_get_adapter,
            patch("cli.commands.plan.Orchestrator") as mock_orch_cls,
        ):
            mock_adapter = MagicMock()
            mock_get_adapter.return_value = mock_adapter

            mock_orch = MagicMock()
            mock_orch.plan = AsyncMock(
                side_effect=AutodevError("planning failed: no spec")
            )
            mock_orch_cls.return_value = mock_orch

            result = runner.invoke(cli, ["plan", "build a widget"])

    assert result.exit_code == 2
    assert "plan failed" in result.output


def test_render_plan_summary_shows_phases() -> None:
    """_render_plan_summary renders all phases and tasks to console."""
    from io import StringIO

    from rich.console import Console

    plan_obj = _make_plan()
    output = StringIO()
    console = Console(file=output, force_terminal=False)

    _render_plan_summary(console, plan_obj)

    rendered = output.getvalue()
    # Check that plan title appears
    assert "Test Plan" in rendered
    # Check that task IDs appear
    assert "1.1" in rendered
    assert "1.2" in rendered
    assert "2.1" in rendered
    # Check that task titles appear
    assert "Create module" in rendered
    assert "Add tests" in rendered
    assert "Wire up API" in rendered
    # Check that files appear
    assert "src/main.py" in rendered
    assert "src/api.py" in rendered
    # Check that plan ID appears in persisted message
    assert "plan-001" in rendered


def test_render_plan_summary_no_title_uses_plan_id() -> None:
    """_render_plan_summary uses plan_id when metadata has no title."""
    from io import StringIO

    from rich.console import Console

    plan_obj = _make_plan()
    plan_obj.metadata = {}  # No title key

    output = StringIO()
    console = Console(file=output, force_terminal=False)

    _render_plan_summary(console, plan_obj)

    rendered = output.getvalue()
    assert "plan-001" in rendered


def test_render_plan_summary_empty_files_shows_dash() -> None:
    """Tasks with no files should show '-' in the Files column."""
    from io import StringIO

    from rich.console import Console

    plan_obj = Plan(
        plan_id="plan-dash",
        spec_hash="abc",
        phases=[
            Phase(
                id="1",
                title="P1",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="No files task",
                        description="desc",
                        files=[],
                    ),
                ],
            ),
        ],
        metadata={"title": "Dash test"},
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )

    output = StringIO()
    console = Console(file=output, force_terminal=False)

    _render_plan_summary(console, plan_obj)

    rendered = output.getvalue()
    assert "-" in rendered
