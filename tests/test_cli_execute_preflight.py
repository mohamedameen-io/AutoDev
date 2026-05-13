"""Preflight tests for ``autodev execute`` (Bug 10).

Mirrors ``test_cli_resume_preflight.py``: a failing healthcheck must abort
``autodev execute`` before any orchestrator work begins.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from cli import cli
from config.defaults import default_config
from config.loader import save_config
from state.schemas import Task


def _write_config(cwd: Path, platform: str = "claude_code") -> None:
    cfg = default_config()
    cfg.platform = platform  # type: ignore[assignment]
    autodev_dir = cwd / ".autodev"
    autodev_dir.mkdir(parents=True, exist_ok=True)
    save_config(cfg, autodev_dir / "config.json")


def test_execute_aborts_with_actionable_message_on_auth_failed(
    tmp_path: Path,
) -> None:
    """When the adapter's preflight healthcheck returns auth_failed,
    ``autodev execute`` must exit 2 with the actionable refresh-auth block.
    """
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)

        mock_adapter = MagicMock()
        mock_adapter.healthcheck = AsyncMock(
            return_value=(
                False,
                "auth_failed: Failed to authenticate. API Error: 403",
            )
        )

        with (
            patch(
                "cli.commands.execute.get_adapter",
                AsyncMock(return_value=mock_adapter),
            ),
            patch("cli.commands.execute.Orchestrator") as mock_orch_cls,
        ):
            result = runner.invoke(cli, ["execute"])

        # Orchestrator must NOT have been built — preflight gates loop entry.
        assert not mock_orch_cls.called

    assert result.exit_code == 2, result.output
    assert "infrastructure not ready" in result.output
    assert "auth_failed" in result.output
    assert "ANTHROPIC_AUTH_TOKEN" in result.output


def test_execute_proceeds_when_healthcheck_passes(tmp_path: Path) -> None:
    """When preflight passes, execute builds the orchestrator and runs."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)

        mock_adapter = MagicMock()
        mock_adapter.healthcheck = AsyncMock(return_value=(True, "ok"))

        tasks = [
            Task(
                id="1.1",
                phase_id="1",
                title="Executed task",
                description="OK",
                status="complete",
                retry_count=0,
            ),
        ]

        with (
            patch(
                "cli.commands.execute.get_adapter",
                AsyncMock(return_value=mock_adapter),
            ),
            patch("cli.commands.execute.Orchestrator") as mock_orch_cls,
            patch("cli.commands.execute.build_registry"),
        ):
            mock_orch = MagicMock()
            mock_orch.execute = AsyncMock(return_value=tasks)
            mock_orch_cls.return_value = mock_orch

            result = runner.invoke(cli, ["execute"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert "1.1" in result.output
    assert mock_adapter.healthcheck.await_count >= 1
