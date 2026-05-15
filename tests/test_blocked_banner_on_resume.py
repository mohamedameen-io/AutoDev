"""Tests for v0.32.0 Phase 5 (Gap G): pre-flight blocked-task banner
shared by ``autodev plan`` / ``autodev execute`` / ``autodev resume``.

The banner helper :func:`cli._blocked_banner._maybe_print_blocked_banner`
is unit-tested directly (the resume / execute / plan integration would
require spinning up an Orchestrator + adapter health probes, which
runs orthogonal codepaths that already have their own tests). The
helper is the only surface every command imports from, so testing the
helper is sufficient to cover the contract.

Two cases:

  * Banner is printed when blocked tasks exist (the message names the
    count and the recovery surface).
  * Banner is silent when no blocked tasks exist (control case).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import io
from pathlib import Path

from rich.console import Console

from cli._blocked_banner import _maybe_print_blocked_banner
from state.plan_manager import PlanManager
from state.schemas import (
    AcceptanceCriterion,
    Phase,
    Plan,
    Task,
)


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _seed_plan(cwd: Path, plan: Plan) -> None:
    pm = PlanManager(cwd, session_id="sess-test-banner")

    async def _init() -> None:
        await pm.init_plan(plan)

    asyncio.run(_init())


def _capture_console() -> tuple[Console, io.StringIO]:
    """A rich Console wired to an in-memory buffer so tests can read
    everything written without spinning up CliRunner."""
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=200)
    return console, buf


def _mk_blocked_plan(n_blocked: int) -> Plan:
    """Plan with ``n_blocked`` blocked tasks and one pending placeholder."""
    tasks: list[Task] = [
        Task(
            id=f"1.{i + 1}",
            phase_id="1",
            title=f"t{i + 1}",
            description="d",
            status="blocked",
            blocked_reason="reviewer NEEDS_CHANGES",
        )
        for i in range(n_blocked)
    ]
    # Add one pending task so the plan isn't structurally degenerate
    # — exercises the banner's "count blocked, ignore others" path.
    tasks.append(
        Task(
            id=f"1.{n_blocked + 1}",
            phase_id="1",
            title="pending-control",
            description="d",
            status="pending",
        )
    )
    return Plan(
        plan_id="p-banner",
        spec_hash="0123456789abcdef",
        phases=[
            Phase(
                id="1",
                title="Setup",
                tasks=tasks,
                acceptance=[AcceptanceCriterion(id="ph-1", description="ok")],
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
    )


def _mk_clean_plan() -> Plan:
    return Plan(
        plan_id="p-banner-clean",
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


def test_resume_prints_blocked_banner(tmp_path: Path) -> None:
    """When the on-disk plan has blocked tasks, the banner helper
    (called from the resume / execute / plan entry points) prints a
    yellow message naming the count and pointing at the recovery
    surface."""
    _seed_plan(tmp_path, _mk_blocked_plan(n_blocked=3))
    console, buf = _capture_console()

    asyncio.run(_maybe_print_blocked_banner(console, tmp_path))

    out = buf.getvalue()
    # Count is rendered.
    assert "3 task" in out
    # Nudge to the structured recovery surface is present.
    assert "autodev status --blocked" in out


def test_resume_no_banner_when_no_blocked(tmp_path: Path) -> None:
    """Control case: no blocked tasks → banner stays silent (no output
    at all). The empty-state branch must not produce noise during
    normal day-to-day execution."""
    _seed_plan(tmp_path, _mk_clean_plan())
    console, buf = _capture_console()

    asyncio.run(_maybe_print_blocked_banner(console, tmp_path))

    assert buf.getvalue() == ""


def test_banner_silent_when_no_plan_on_disk(tmp_path: Path) -> None:
    """No plan persisted → banner stays silent. Banner must be
    informational-only and never abort the calling command, including
    when invoked before ``autodev plan`` has ever run."""
    console, buf = _capture_console()

    # No PlanManager.init_plan call — empty workspace.
    asyncio.run(_maybe_print_blocked_banner(console, tmp_path))

    assert buf.getvalue() == ""


def test_banner_silent_on_load_failure(tmp_path: Path) -> None:
    """Corrupt ledger → banner swallows the error and stays silent.
    The load layer's own error reporting is the right surface for that
    failure; the banner is informational only."""
    # Simulate a corrupt ledger: write garbage bytes that the loader
    # cannot parse. The banner must NOT raise.
    autodev_dir = tmp_path / ".autodev"
    autodev_dir.mkdir(parents=True, exist_ok=True)
    (autodev_dir / "plan-ledger.jsonl").write_text(
        "this is not valid jsonl\n", encoding="utf-8"
    )

    console, buf = _capture_console()
    asyncio.run(_maybe_print_blocked_banner(console, tmp_path))

    # Either silent (load returned None / raised, both swallowed) — the
    # contract is "no exception escapes". Empty buffer is fine.
    assert "Traceback" not in buf.getvalue()
