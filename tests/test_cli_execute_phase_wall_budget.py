"""Task 2: CLI-surface handling for ``ExecutePhaseWallBudgetExceededError``.

Both ``autodev execute`` and ``autodev resume`` must treat a cumulative
execute-phase wall-clock budget breach as INTERRUPTED (exit 1, "run resume"),
NOT as a genuine failure (exit 2) — mirroring the ``PhaseStuckError`` contract.
The new ``except`` clause must precede the generic ``AutodevError`` handler
(the new error subclasses ``AutodevError``).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from cli import cli
from config.defaults import default_config
from config.loader import save_config
from errors import ExecutePhaseWallBudgetExceededError


def _write_config(cwd: Path, platform: str = "claude_code") -> None:
    cfg = default_config()
    cfg.platform = platform  # type: ignore[assignment]
    autodev_dir = cwd / ".autodev"
    autodev_dir.mkdir(parents=True, exist_ok=True)
    save_config(cfg, autodev_dir / "config.json")


def _wall_budget_exc() -> ExecutePhaseWallBudgetExceededError:
    return ExecutePhaseWallBudgetExceededError(
        "execute-phase wall-clock budget of 100s exceeded after 150.0s",
        budget_s=100.0,
        elapsed_s=150.0,
    )


def test_execute_cli_wall_budget_exits_1(tmp_path: Path) -> None:
    """``autodev execute`` exits 1 (interrupted) + surfaces the resume hint."""
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
            mock_get_adapter.return_value = (
                mock_adapter,
                {"platform": "claude_code"},
            )

            mock_orch = MagicMock()
            mock_orch.execute = AsyncMock(side_effect=_wall_budget_exc())
            mock_orch_cls.return_value = mock_orch

            result = runner.invoke(cli, ["execute"])

    assert result.exit_code == 1, (
        f"expected exit 1 for wall-budget breach, got {result.exit_code}; "
        f"output: {result.output!r}"
    )
    assert "wall-clock budget" in result.output
    assert "resume" in result.output


def test_resume_cli_wall_budget_exits_1(tmp_path: Path) -> None:
    """``autodev resume`` exits 1 (interrupted) + surfaces the resume hint."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)

        with (
            patch("cli.commands.resume.get_adapter") as mock_get_adapter,
            patch("cli.commands.resume.Orchestrator") as mock_orch_cls,
        ):
            mock_adapter = MagicMock()
            mock_adapter.healthcheck = AsyncMock(return_value=(True, "ok"))
            mock_get_adapter.return_value = (
                mock_adapter,
                {"platform": "claude_code"},
            )

            mock_orch = MagicMock()
            mock_orch.resume = AsyncMock(side_effect=_wall_budget_exc())
            mock_orch_cls.return_value = mock_orch

            result = runner.invoke(cli, ["resume"])

    assert result.exit_code == 1, (
        f"expected exit 1 for wall-budget breach, got {result.exit_code}; "
        f"output: {result.output!r}"
    )
    assert "wall-clock budget" in result.output
    assert "resume" in result.output
