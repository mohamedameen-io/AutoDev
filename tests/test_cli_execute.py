"""Tests for ``autodev execute`` CLI command."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner
from rich.console import Console

from cli import cli
from cli.commands.execute import _render_execute_summary
from config.defaults import default_config
from config.loader import save_config
from errors import AutodevError, PhaseStuckError
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


def _make_tasks() -> list[Task]:
    """Return a list of tasks with various statuses for rendering tests."""
    return [
        Task(
            id="1.1",
            phase_id="1",
            title="Done task",
            description="Completed",
            status="complete",
            retry_count=0,
            escalated=False,
        ),
        Task(
            id="1.2",
            phase_id="1",
            title="Blocked task",
            description="Blocked",
            status="blocked",
            retry_count=3,
            escalated=True,
        ),
        Task(
            id="2.1",
            phase_id="2",
            title="Skipped task",
            description="Skipped",
            status="skipped",
            retry_count=1,
            escalated=False,
        ),
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_execute_missing_config_exits_1(tmp_path: Path) -> None:
    """Execute in a directory without .autodev/config.json should exit 1."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(cli, ["execute"])
    assert result.exit_code == 1
    assert "autodev init" in result.output


def test_execute_invalid_config_exits_1(tmp_path: Path) -> None:
    """Execute with invalid JSON config should exit 1."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        autodev_dir = cwd / ".autodev"
        autodev_dir.mkdir(parents=True, exist_ok=True)
        (autodev_dir / "config.json").write_text("broken json {", encoding="utf-8")

        result = runner.invoke(cli, ["execute"])
    assert result.exit_code == 1
    assert "config error" in result.output


def test_execute_dry_run_without_plan_exits_1(tmp_path: Path) -> None:
    """Execute --dry-run with config but no plan exits 1 with a hint
    (v0.25.2 replaced the warning-only stub with real plan preview).
    """
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)

        result = runner.invoke(cli, ["execute", "--dry-run"], catch_exceptions=False)

    assert result.exit_code == 1
    assert "plan" in result.output.lower()


def test_execute_success_renders_table(tmp_path: Path) -> None:
    """Execute succeeds and renders a task table."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)

        tasks = _make_tasks()

        with (
            patch("cli.commands.execute.get_adapter") as mock_get_adapter,
            patch("cli.commands.execute.Orchestrator") as mock_orch_cls,
        ):
            mock_adapter = MagicMock()
            # v0.28.0 (Bug 10): preflight re-probe must succeed.
            mock_adapter.healthcheck = AsyncMock(return_value=(True, "ok"))
            # v0.38.0 HK10: get_adapter returns (adapter, selection_meta).
            mock_get_adapter.return_value = (mock_adapter, {"platform": "claude_code"})

            mock_orch = MagicMock()
            mock_orch.execute = AsyncMock(return_value=tasks)
            mock_orch_cls.return_value = mock_orch

            result = runner.invoke(cli, ["execute"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert "3 tasks" in result.output
    assert "1.1" in result.output
    assert "1.2" in result.output
    assert "2.1" in result.output


def test_execute_autodev_error_exits_2(tmp_path: Path) -> None:
    """Execute exits 2 when orchestrator raises AutodevError."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)

        with (
            patch("cli.commands.execute.get_adapter") as mock_get_adapter,
            patch("cli.commands.execute.Orchestrator") as mock_orch_cls,
        ):
            mock_adapter = MagicMock()
            # v0.28.0 (Bug 10): preflight re-probe must succeed.
            mock_adapter.healthcheck = AsyncMock(return_value=(True, "ok"))
            # v0.38.0 HK10: get_adapter returns (adapter, selection_meta).
            mock_get_adapter.return_value = (mock_adapter, {"platform": "claude_code"})

            mock_orch = MagicMock()
            mock_orch.execute = AsyncMock(
                side_effect=AutodevError("no plan found")
            )
            mock_orch_cls.return_value = mock_orch

            result = runner.invoke(cli, ["execute"])

    assert result.exit_code == 2
    assert "execute failed" in result.output


def test_execute_phase_stuck_error_exits_1(tmp_path: Path) -> None:
    """PhaseStuckError exits 1 (interrupted), not 2 (genuine failure).

    A PhaseStuckError means the run was interrupted and tasks are wedged —
    the operator should resume, not treat this as a broken plan/adapter.
    Exit 1 distinguishes "interrupted run" from "genuine failure" (exit 2).
    """
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)

        with (
            patch("cli.commands.execute.get_adapter") as mock_get_adapter,
            patch("cli.commands.execute.Orchestrator") as mock_orch_cls,
        ):
            mock_adapter = MagicMock()
            mock_adapter.healthcheck = AsyncMock(return_value=(True, "ok"))
            mock_get_adapter.return_value = (mock_adapter, {"platform": "claude_code"})

            mock_orch = MagicMock()
            mock_orch.execute = AsyncMock(
                side_effect=PhaseStuckError("phase-1", ["1.1"])
            )
            mock_orch_cls.return_value = mock_orch

            result = runner.invoke(cli, ["execute"])

    assert result.exit_code == 1, (
        f"expected exit 1 for PhaseStuckError, got {result.exit_code}; "
        f"output: {result.output!r}"
    )
    assert "interrupted" in result.output


def test_execute_with_task_id(tmp_path: Path) -> None:
    """Execute passes --task to the orchestrator."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)

        task = Task(
            id="1.1",
            phase_id="1",
            title="Target task",
            description="Specific",
            status="complete",
            retry_count=0,
            escalated=False,
        )

        with (
            patch("cli.commands.execute.get_adapter") as mock_get_adapter,
            patch("cli.commands.execute.Orchestrator") as mock_orch_cls,
        ):
            mock_adapter = MagicMock()
            # v0.28.0 (Bug 10): preflight re-probe must succeed.
            mock_adapter.healthcheck = AsyncMock(return_value=(True, "ok"))
            # v0.38.0 HK10: get_adapter returns (adapter, selection_meta).
            mock_get_adapter.return_value = (mock_adapter, {"platform": "claude_code"})

            mock_orch = MagicMock()
            mock_orch.execute = AsyncMock(return_value=[task])
            mock_orch_cls.return_value = mock_orch

            result = runner.invoke(
                cli, ["execute", "--task", "1.1"], catch_exceptions=False
            )

    assert result.exit_code == 0, result.output
    mock_orch.execute.assert_awaited_once_with(task_id="1.1")


def test_delivered_approved_finalization_exits_0(tmp_path: Path) -> None:
    """Delivered+APPROVED finalization path exits 0 (NOT 1 or 2).

    This is the core A4 requirement: when the orchestrator finalizes a task
    via the delivered+approved path (_maybe_accept_approved_on_exhaustion
    marks it complete), the CLI MUST exit 0.

    The test stubs the orchestrator's execute() to return a task list that
    includes a ``complete`` task — the exact output state produced by
    _maybe_accept_approved_on_exhaustion after flushing the approved diff to
    main. No PhaseStuckError is raised (the task is terminal), and no
    AutodevError is raised (the run succeeded), so the CLI must exit 0.

    Regression guard: pre-A4 this path raised PhaseStuckError (→ exit 1) or
    AutodevError (→ exit 2), silently losing the approved fix.
    """
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)

        # Task returned by the orchestrator after approved-on-exhaustion
        # finalization: status is "complete" (the terminal state
        # _maybe_accept_approved_on_exhaustion walks to).
        delivered_and_finalized = Task(
            id="0.1",
            phase_id="0",
            title="Confirm something",
            description="Research task",
            status="complete",
            retry_count=0,
            escalated=False,
        )

        with (
            patch("cli.commands.execute.get_adapter") as mock_get_adapter,
            patch("cli.commands.execute.Orchestrator") as mock_orch_cls,
        ):
            mock_adapter = MagicMock()
            mock_adapter.healthcheck = AsyncMock(return_value=(True, "ok"))
            mock_get_adapter.return_value = (mock_adapter, {"platform": "claude_code"})

            mock_orch = MagicMock()
            # execute() returns the finalized task (complete) — NO exception raised.
            mock_orch.execute = AsyncMock(return_value=[delivered_and_finalized])
            mock_orch_cls.return_value = mock_orch

            result = runner.invoke(cli, ["execute"], catch_exceptions=False)

    assert result.exit_code == 0, (
        f"delivered+approved finalization must exit 0, got {result.exit_code}; "
        f"output: {result.output!r}"
    )


# ---------------------------------------------------------------------------
# Direct _render_execute_summary tests
# ---------------------------------------------------------------------------


def test_render_execute_summary_no_tasks() -> None:
    """_render_execute_summary with empty list shows 'No tasks' message."""
    output = StringIO()
    console = Console(file=output, force_terminal=False)

    _render_execute_summary(console, [])

    rendered = output.getvalue()
    assert "No tasks to execute" in rendered


def test_render_execute_summary_with_tasks() -> None:
    """_render_execute_summary renders tasks with correct statuses and colors."""
    output = StringIO()
    console = Console(file=output, force_terminal=False)

    tasks = _make_tasks()
    _render_execute_summary(console, tasks)

    rendered = output.getvalue()
    assert "3 tasks" in rendered
    assert "1.1" in rendered
    assert "1.2" in rendered
    assert "2.1" in rendered
    assert "complete" in rendered
    assert "blocked" in rendered
    assert "skipped" in rendered
    # Check escalation column
    assert "yes" in rendered  # task 1.2 is escalated
    assert "no" in rendered   # task 1.1 is not


def test_render_execute_summary_single_task() -> None:
    """_render_execute_summary works with a single task."""
    output = StringIO()
    console = Console(file=output, force_terminal=False)

    task = Task(
        id="3.1",
        phase_id="3",
        title="Solo task",
        description="Just one",
        status="complete",
        retry_count=0,
        escalated=False,
    )

    _render_execute_summary(console, [task])

    rendered = output.getvalue()
    assert "1 tasks" in rendered
    assert "3.1" in rendered
