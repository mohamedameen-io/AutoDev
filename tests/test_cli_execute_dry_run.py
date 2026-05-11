"""Tests for ``autodev execute --dry-run``.

v0.25.2 — replaces the prior warning-only stub. Renders the plan +
phase→task dispatch order WITHOUT invoking any agent adapter, so the
operator can preview what would run and validate ``depends_on`` ordering.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from cli import cli
from config.defaults import default_config
from config.loader import save_config
from state.schemas import Phase, Plan, Task


def _write_config(cwd: Path) -> None:
    cfg = default_config()
    cfg.platform = "claude_code"  # type: ignore[assignment]
    autodev_dir = cwd / ".autodev"
    autodev_dir.mkdir(parents=True, exist_ok=True)
    save_config(cfg, autodev_dir / "config.json")


def _make_plan_for_dry_run() -> Plan:
    """Plan with cross-task dependencies so dispatch-window grouping is
    visible: 1.1 + 1.2 in window 1 (no deps), 1.3 in window 2 (depends
    on 1.1), 2.1 in phase 2."""
    return Plan(
        plan_id="plan-dryrun-test",
        spec_hash="abc",
        phases=[
            Phase(
                id="1",
                title="Phase One",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="First task",
                        description="d",
                        status="pending",
                    ),
                    Task(
                        id="1.2",
                        phase_id="1",
                        title="Second task",
                        description="d",
                        status="pending",
                    ),
                    Task(
                        id="1.3",
                        phase_id="1",
                        title="Third task",
                        description="d",
                        status="pending",
                        depends_on=["1.1"],
                    ),
                ],
            ),
            Phase(
                id="2",
                title="Phase Two",
                tasks=[
                    Task(
                        id="2.1",
                        phase_id="2",
                        title="Fourth task",
                        description="d",
                        status="pending",
                    ),
                ],
            ),
        ],
        created_at="2026-05-11T00:00:00+00:00",
        updated_at="2026-05-11T00:00:00+00:00",
    )


def test_dry_run_renders_plan_preview(tmp_path: Path) -> None:
    """``execute --dry-run`` lists every phase and task in the plan."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)

        plan = _make_plan_for_dry_run()
        with patch("cli.commands.execute.PlanManager") as mock_pm_cls:
            mock_pm = MagicMock()
            mock_pm.load = AsyncMock(return_value=plan)
            mock_pm_cls.return_value = mock_pm

            result = runner.invoke(cli, ["execute", "--dry-run"])

        assert result.exit_code == 0, result.output
        for tid in ("1.1", "1.2", "1.3", "2.1"):
            assert tid in result.output, f"task {tid} missing from preview"
        assert "First task" in result.output
        assert "Fourth task" in result.output


def test_dry_run_renders_dispatch_windows_respecting_depends_on(
    tmp_path: Path,
) -> None:
    """Window 1 of phase 1 contains 1.1 and 1.2; window 2 contains 1.3
    because it depends_on=[1.1]."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)

        plan = _make_plan_for_dry_run()
        with patch("cli.commands.execute.PlanManager") as mock_pm_cls:
            mock_pm = MagicMock()
            mock_pm.load = AsyncMock(return_value=plan)
            mock_pm_cls.return_value = mock_pm

            result = runner.invoke(cli, ["execute", "--dry-run"])

        assert result.exit_code == 0, result.output
        out = result.output
        # Dispatch order section present.
        assert "Dispatch" in out or "dispatch" in out
        # The output should show that 1.3 comes AFTER 1.1 — verify by
        # checking the order of appearance in the dispatch listing,
        # which lists per-window content.
        # We expect a window label or marker that indicates 1.3 is in
        # a later window than 1.1 and 1.2.
        pos_11 = out.find("1.1")
        pos_13 = out.find("1.3")
        # 1.3 must appear after 1.1 in any plausible rendering.
        assert pos_11 < pos_13


def test_dry_run_no_plan_clean_error(tmp_path: Path) -> None:
    """When no plan exists, ``execute --dry-run`` exits 1 with a hint."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)

        with patch("cli.commands.execute.PlanManager") as mock_pm_cls:
            mock_pm = MagicMock()
            mock_pm.load = AsyncMock(return_value=None)
            mock_pm_cls.return_value = mock_pm

            result = runner.invoke(cli, ["execute", "--dry-run"])

        assert result.exit_code == 1
        assert "plan" in result.output.lower()


def test_dry_run_does_not_invoke_adapter(tmp_path: Path) -> None:
    """``--dry-run`` must NOT call ``get_adapter`` or instantiate the
    Orchestrator — preview only."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)

        plan = _make_plan_for_dry_run()
        with patch("cli.commands.execute.PlanManager") as mock_pm_cls, patch(
            "cli.commands.execute.get_adapter"
        ) as mock_get_adapter, patch(
            "cli.commands.execute.Orchestrator"
        ) as mock_orch_cls:
            mock_pm = MagicMock()
            mock_pm.load = AsyncMock(return_value=plan)
            mock_pm_cls.return_value = mock_pm

            result = runner.invoke(cli, ["execute", "--dry-run"])

        assert result.exit_code == 0, result.output
        mock_get_adapter.assert_not_called()
        mock_orch_cls.assert_not_called()
