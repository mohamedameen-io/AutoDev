"""Tests for v0.32.0 Phase 5 (Gap G): doctor's blocked-tasks section.

The :func:`cli.commands.doctor._render_blocked_tasks_section` reads the
on-disk plan, counts blocked tasks, and prints a one-line summary
(green when zero, yellow with count + nudge to ``autodev status
--blocked`` when > 0). The section runs alongside the rest of doctor
so failures must NOT cascade — the helper degrades to a dim error line
on any load failure.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

from click.testing import CliRunner

from cli import cli
from config.defaults import default_config
from config.loader import save_config
from state.schemas import (
    AcceptanceCriterion,
    Phase,
    Plan,
    Task,
)


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _write_config(cwd: Path) -> None:
    cfg = default_config()
    cfg.platform = "claude_code"  # type: ignore[assignment]
    autodev_dir = cwd / ".autodev"
    autodev_dir.mkdir(parents=True, exist_ok=True)
    save_config(cfg, autodev_dir / "config.json")


def _seed_plan(cwd: Path, plan: Plan) -> None:
    import asyncio

    from state.plan_manager import PlanManager

    pm = PlanManager(cwd, session_id="sess-test-doctor-blocked")

    async def _init() -> None:
        await pm.init_plan(plan)

    asyncio.run(_init())


def _mk_clean_plan() -> Plan:
    return Plan(
        plan_id="p-doctor-clean",
        spec_hash="0123456789abcdef",
        phases=[
            Phase(
                id="1",
                title="Setup",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="t1",
                        description="d1",
                        status="pending",
                    ),
                ],
                acceptance=[AcceptanceCriterion(id="ph-1", description="ok")],
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
    )


def _mk_two_blocked_plan() -> Plan:
    return Plan(
        plan_id="p-doctor-two-blocked",
        spec_hash="0123456789abcdef",
        phases=[
            Phase(
                id="1",
                title="Setup",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="t1",
                        description="d1",
                        status="blocked",
                        blocked_reason="reviewer NEEDS_CHANGES",
                    ),
                    Task(
                        id="1.2",
                        phase_id="1",
                        title="t2",
                        description="d2",
                        status="blocked",
                        blocked_reason="tests failed",
                    ),
                ],
                acceptance=[AcceptanceCriterion(id="ph-1", description="ok")],
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
    )


def test_doctor_reports_zero_blocked(tmp_path: Path) -> None:
    """``autodev doctor`` reports "Blocked tasks: none" when the plan
    has no blocked tasks."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)
        _seed_plan(cwd, _mk_clean_plan())

        # Doctor returns exit code 0 only when ALL its CLI probes pass
        # (claude/cursor on PATH). In CI / sandboxed test environments
        # those probes commonly fail, so we accept either 0 or 1 here —
        # what we care about is that the blocked-tasks section ran and
        # printed the expected text alongside whatever else doctor said.
        result = runner.invoke(cli, ["doctor"])

        assert result.exit_code in (0, 1), result.output
        assert "Blocked tasks: none" in result.output


def test_doctor_reports_n_blocked(tmp_path: Path) -> None:
    """``autodev doctor`` reports the blocked-task count and points to
    ``autodev status --blocked`` when blocked tasks exist."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)
        _seed_plan(cwd, _mk_two_blocked_plan())

        result = runner.invoke(cli, ["doctor"])

        assert result.exit_code in (0, 1), result.output
        assert "Blocked tasks:" in result.output
        # The exact count of 2 should appear in the message body.
        assert "2 task" in result.output
        # The nudge to the structured surface is present.
        assert "autodev status --blocked" in result.output
