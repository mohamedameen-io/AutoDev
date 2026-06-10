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


# ---------------------------------------------------------------------------
# v0.36.0 F3: status --blocked surfaces recovery outcomes + dump paths.
# ---------------------------------------------------------------------------


def _make_blocked_plan() -> Plan:
    import datetime as _dt

    task = Task(
        id="1.1",
        phase_id="1",
        title="blocked task",
        description="x",
        files=[],
        acceptance=[],
        status="blocked",
        blocked_reason="something went wrong",
    )
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    return Plan(
        plan_id="p-1",
        spec_hash="h" * 16,
        metadata={"title": "test plan"},
        edit_scope=[],
        phases=[
            Phase(
                id="1",
                title="Phase 1",
                tasks=[task],
                edit_scope=None,
            )
        ],
        created_at=now,
        updated_at=now,
    )


def test_status_blocked_renders_recovery_outcomes(tmp_path: Path) -> None:
    from io import StringIO
    import json as _json

    from cli.commands.status import _render_blocked_section

    # Synthesise the plan + ledger lines the F3 helpers parse.
    (tmp_path / ".autodev").mkdir(parents=True)
    ledger = tmp_path / ".autodev" / "plan-ledger.jsonl"
    rows = [
        {
            "op": "recovery_tier_attempted",
            "payload": {
                "tier": 4,
                "outcome": "applied",
                "reason": "recurrent_path_failure",
                "from_state": "undegraded",
                "to_state": "dropped:notes/foo.md",
            },
        },
        {
            "op": "architect_attempt_failed",
            "payload": {
                "attempt": 1,
                "model": "claude-opus-4-7",
                "duration_s": 1.5,
                "rejection_count": 2,
                "primary_class": "new_md_deliverable",
            },
        },
    ]
    with ledger.open("w") as fh:
        for r in rows:
            fh.write(_json.dumps(r) + "\n")

    plan = _make_blocked_plan()
    out = StringIO()
    console = Console(file=out, force_terminal=False)
    _render_blocked_section(console, plan, cwd=tmp_path)
    rendered = out.getvalue()
    assert "Recovery Tier Outcomes" in rendered
    assert "Architect Attempts" in rendered
    assert "applied" in rendered
    assert "claude-opus" in rendered


def test_status_blocked_lists_dump_paths(tmp_path: Path) -> None:
    from io import StringIO

    from cli.commands.status import _render_blocked_section

    (tmp_path / ".autodev" / "debug").mkdir(parents=True)
    d1 = tmp_path / ".autodev" / "debug" / "architect-failed-1000.md"
    d2 = tmp_path / ".autodev" / "debug" / "architect-failed-2000.md"
    d1.write_text("# rejected attempt 1\n")
    d2.write_text("# rejected attempt 2\n")

    plan = _make_blocked_plan()
    out = StringIO()
    console = Console(file=out, force_terminal=False, width=200)
    _render_blocked_section(console, plan, cwd=tmp_path)
    rendered = out.getvalue()
    assert "Archived Rejected Plans" in rendered
    assert "architect-failed-" in rendered


# ---------------------------------------------------------------------------
# v0.38.0 I2 (HK4): capped-phases panel on `status --blocked`.
# ---------------------------------------------------------------------------


def _make_capped_plan() -> Plan:
    """Plan with two capped phases (one phase-scope, one plan-scope)
    plus one accepted phase. No blocked tasks — exercises the case
    where the capped-phases panel renders standalone."""
    import datetime as _dt

    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    return Plan(
        plan_id="p-capped-status",
        spec_hash="h" * 16,
        metadata={"title": "capped status plan"},
        edit_scope=[],
        phases=[
            Phase(
                id="1",
                title="Phase one",
                review_status="capped",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="t1",
                        description="x",
                        status="complete",
                    ),
                ],
            ),
            Phase(
                id="2",
                title="Phase two",
                review_status="capped",
                tasks=[
                    Task(
                        id="2.1",
                        phase_id="2",
                        title="t2",
                        description="x",
                        status="blocked",
                        blocked_reason="qa fail",
                    ),
                ],
            ),
            Phase(
                id="3",
                title="Phase three",
                review_status="accepted",
                tasks=[
                    Task(
                        id="3.1",
                        phase_id="3",
                        title="t3",
                        description="x",
                        status="complete",
                    ),
                ],
            ),
        ],
        created_at=now,
        updated_at=now,
    )


def test_status_blocked_renders_capped_phases_panel(tmp_path: Path) -> None:
    """When the plan has phases at ``review_status='capped'`` and the
    ledger carries ``corrective_cap_reached`` ops, the blocked surface
    renders a "Capped phases" panel with per-phase scope + fire counts
    + the bulk recovery command."""
    from io import StringIO
    import json as _json

    from cli.commands.status import _render_blocked_section

    (tmp_path / ".autodev").mkdir(parents=True)
    ledger = tmp_path / ".autodev" / "plan-ledger.jsonl"
    # Phase 1: two phase-scope cap fires; Phase 2: one plan-scope.
    rows = [
        {"op": "corrective_cap_reached",
         "payload": {"phase_id": "1", "scope": "phase", "cap": 8, "dropped": 2}},
        {"op": "corrective_cap_reached",
         "payload": {"phase_id": "1", "scope": "phase", "cap": 8, "dropped": 1}},
        {"op": "corrective_cap_reached",
         "payload": {"phase_id": "2", "scope": "plan", "cap": 24, "dropped": 3}},
    ]
    with ledger.open("w") as fh:
        for r in rows:
            fh.write(_json.dumps(r) + "\n")

    plan = _make_capped_plan()
    out = StringIO()
    console = Console(file=out, force_terminal=False, width=140)
    _render_blocked_section(console, plan, cwd=tmp_path)
    rendered = out.getvalue()

    # Headline: count + scope breakdown.
    assert "Capped phases" in rendered
    assert "2 phase(s) capped" in rendered
    assert "phase-scope" in rendered and "plan-scope" in rendered
    # Bulk recovery suggested command appears verbatim.
    assert "autodev requeue --capped-phases" in rendered
    # Per-phase rows: ids + fire counts + scope labels.
    assert "Phase one" in rendered
    assert "Phase two" in rendered
    # Phase 1 has 2 cap fires; phase 2 has 1. Sort by count desc → 1
    # appears before 2 in the table body.
    p1_idx = rendered.find("Phase one")
    p2_idx = rendered.find("Phase two")
    assert 0 < p1_idx < p2_idx


def test_status_blocked_no_capped_phases_no_panel(tmp_path: Path) -> None:
    """Back-compat: plans without capped phases do not render the new
    panel — neither the "Capped phases" title nor the bulk recovery
    command appear in the output."""
    from io import StringIO

    from cli.commands.status import _render_blocked_section

    (tmp_path / ".autodev").mkdir(parents=True)
    plan = _make_blocked_plan()  # blocked task, no capped phases
    out = StringIO()
    console = Console(file=out, force_terminal=False, width=140)
    _render_blocked_section(console, plan, cwd=tmp_path)
    rendered = out.getvalue()

    assert "Capped phases" not in rendered
    assert "autodev requeue --capped-phases" not in rendered


def test_status_blocked_shows_design_class_action(tmp_path: Path) -> None:
    from io import StringIO
    import json as _json

    from cli.commands.status import _render_blocked_section

    (tmp_path / ".autodev").mkdir(parents=True)
    ledger = tmp_path / ".autodev" / "plan-ledger.jsonl"
    with ledger.open("w") as fh:
        fh.write(
            _json.dumps(
                {
                    "op": "path_rejection_recorded",
                    "payload": {
                        "task_id": "",
                        "path": "notes/foo.md",
                        "class": "new_md_deliverable",
                    },
                }
            )
            + "\n"
        )

    plan = _make_blocked_plan()
    out = StringIO()
    console = Console(file=out, force_terminal=False)
    _render_blocked_section(console, plan, cwd=tmp_path)
    rendered = out.getvalue()
    # The new_md_deliverable diagnosis paragraph mentions both options.
    assert "Action hint" in rendered or "Choose ONE" in rendered
