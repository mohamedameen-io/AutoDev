"""Preflight tests for ``autodev resume`` (Bug 10).

The resume command must re-run ``adapter.healthcheck()`` (NOT the cached
result from ``get_adapter``) right before entering the execute loop. If the
probe fails, exit 2 with an actionable, multi-line message.
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
    """Write a minimal valid config.json into <cwd>/.autodev/."""
    cfg = default_config()
    cfg.platform = platform  # type: ignore[assignment]
    autodev_dir = cwd / ".autodev"
    autodev_dir.mkdir(parents=True, exist_ok=True)
    save_config(cfg, autodev_dir / "config.json")


def test_resume_aborts_with_actionable_message_on_auth_failed(
    tmp_path: Path,
) -> None:
    """When the adapter's preflight healthcheck returns auth_failed,
    ``autodev resume`` must exit 2 and print the actionable refresh-auth
    block (mentioning ANTHROPIC_AUTH_TOKEN as one of the bullets).
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
            patch("cli.commands.resume.get_adapter", AsyncMock(return_value=mock_adapter)),
            patch("cli.commands.resume.Orchestrator") as mock_orch_cls,
        ):
            result = runner.invoke(cli, ["resume"])

        # Orchestrator must NOT have been built — preflight gates loop entry.
        assert not mock_orch_cls.called

    assert result.exit_code == 2, result.output
    assert "infrastructure not ready" in result.output
    assert "auth_failed" in result.output
    # At least one of the actionable bullets must mention ANTHROPIC_AUTH_TOKEN.
    assert "ANTHROPIC_AUTH_TOKEN" in result.output


def test_resume_proceeds_when_healthcheck_passes(tmp_path: Path) -> None:
    """When preflight passes, resume builds the orchestrator and runs."""
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
                title="Resumed task",
                description="Resumed OK",
                status="complete",
                retry_count=0,
            ),
        ]

        with (
            patch("cli.commands.resume.get_adapter", AsyncMock(return_value=mock_adapter)),
            patch("cli.commands.resume.Orchestrator") as mock_orch_cls,
        ):
            mock_orch = MagicMock()
            mock_orch.resume = AsyncMock(return_value=tasks)
            mock_orch_cls.return_value = mock_orch

            result = runner.invoke(cli, ["resume"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert "1.1" in result.output
    # Re-probe must have happened (mandatory by plan).
    assert mock_adapter.healthcheck.await_count >= 1
