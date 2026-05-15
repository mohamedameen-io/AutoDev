"""Tests for v0.32.0 Phase 5 (Gap G): ``autodev status --blocked`` flag.

The flag (a) renders structured recovery panels for every blocked
task, and (b) returns 0 even when no blocked tasks exist (informational
surface). Default ``autodev status`` (no flag) is also tested for the
"N blocked task(s) — run autodev status --blocked" nudge that was
added alongside the flag.
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
    RecoveryHint,
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

    pm = PlanManager(cwd, session_id="sess-test-status-blocked")

    async def _init() -> None:
        await pm.init_plan(plan)

    asyncio.run(_init())


def _mk_blocked_plan_with_hint() -> Plan:
    """Plan with one blocked task carrying a populated RecoveryHint."""
    hint = RecoveryHint(
        class_="thin_review_evidence",
        recommended_user_action=(
            "Inspect the rejection in evidence/1.1-review.json and update "
            "the implementation, then `autodev requeue --task 1.1`."
        ),
        relevant_evidence_files=[
            ".autodev/evidence/1.1-review.json",
            ".autodev/evidence/1.1-coder.json",
        ],
        relevant_debug_files=[
            ".autodev/debug/1.1-empty-review.json",
        ],
        commands_to_try=[
            "autodev requeue --task 1.1",
            "autodev status --blocked",
        ],
    )
    return Plan(
        plan_id="p-status-blocked",
        spec_hash="0123456789abcdef",
        phases=[
            Phase(
                id="1",
                title="Setup",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="add foo",
                        description="add src/foo.py",
                        status="blocked",
                        blocked_reason="reviewer NEEDS_CHANGES",
                        block_reason_class="verdict",
                        retry_count=3,
                        recovery_hint=hint,
                    ),
                ],
                acceptance=[AcceptanceCriterion(id="ph-1", description="ok")],
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
    )


def _mk_clean_plan() -> Plan:
    return Plan(
        plan_id="p-status-clean",
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
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
    )


def test_status_blocked_renders_hints(tmp_path: Path) -> None:
    """``autodev status --blocked`` prints the structured recovery
    surface: block class, reason, recommended action, evidence file
    paths, and commands."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)
        _seed_plan(cwd, _mk_blocked_plan_with_hint())

        result = runner.invoke(cli, ["status", "--blocked"])

        assert result.exit_code == 0, result.output
        out = result.output
        # Structured surface: block class label is shown.
        assert "Block class:" in out
        # Recommended action carries the body text.
        assert "Recommended action:" in out
        # At least one evidence file path appears.
        assert ".autodev/evidence/1.1-review.json" in out
        # At least one command appears for copy-paste.
        assert "autodev requeue --task 1.1" in out


def test_status_blocked_empty_when_no_blocked(tmp_path: Path) -> None:
    """When the plan has no blocked tasks, ``autodev status --blocked``
    prints a clean "No blocked tasks." message and exits 0."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)
        _seed_plan(cwd, _mk_clean_plan())

        result = runner.invoke(cli, ["status", "--blocked"])

        assert result.exit_code == 0, result.output
        assert "No blocked tasks" in result.output


def test_status_default_nudges_to_blocked_flag_when_blocked_exist(
    tmp_path: Path,
) -> None:
    """Default ``autodev status`` (no flag) still surfaces a nudge to
    ``status --blocked`` when blocked tasks exist."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)
        _seed_plan(cwd, _mk_blocked_plan_with_hint())

        result = runner.invoke(cli, ["status"])

        assert result.exit_code == 0, result.output
        assert "blocked task" in result.output.lower()
        assert "autodev status --blocked" in result.output
