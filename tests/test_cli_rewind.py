"""Tests for ``autodev rewind`` CLI command (v0.29.0 Bug 9).

Three end-to-end happy / unhappy paths through ``CliRunner``:

  - ``--dry-run`` writes zero ledger entries.
  - "no stable phase to rewind to" exits 1 with an actionable
    suggestion.
  - the full happy path (auto-detect target + ``--yes`` + apply) flips
    later phases back to ``pending`` and writes the audit
    ``op="rewind"`` ledger breadcrumb.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
from pathlib import Path

from click.testing import CliRunner

from cli import cli
from config.defaults import default_config
from config.loader import save_config
from state.ledger import append_entry
from state.plan_manager import PlanManager
from state.schemas import Phase, Plan, Task


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _write_config(cwd: Path) -> None:
    cfg = default_config()
    cfg.platform = "claude_code"  # type: ignore[assignment]
    autodev_dir = cwd / ".autodev"
    autodev_dir.mkdir(parents=True, exist_ok=True)
    save_config(cfg, autodev_dir / "config.json")


def _seed_plan(cwd: Path, plan: Plan) -> None:
    pm = PlanManager(cwd, session_id="sess-cli-rewind-init")

    async def _init() -> None:
        await pm.init_plan(plan)

    asyncio.run(_init())


def _seed_real_acceptance(cwd: Path, phase_id: str) -> None:
    """Append a phase_review_complete (accept_phase=True) followed by an
    update_phase_meta(review_status="accepted") so the detector treats
    ``phase_id`` as a genuinely-accepted stable phase."""

    async def _go() -> None:
        await append_entry(
            cwd,
            op="phase_review_complete",
            payload={
                "tournament_id": f"t-{phase_id}",
                "phase_id": phase_id,
                "passes": 1,
                "winner": "A",
                "accept_phase": True,
                "artifact_dir": f".autodev/tournaments/phase-review-{phase_id}",
            },
            session_id="sess-cli-rewind-review",
        )
        pm = PlanManager(cwd, session_id="sess-cli-rewind-accept")
        await pm.update_phase_meta(phase_id, review_status="accepted")

    asyncio.run(_go())


def _read_ledger_ops(cwd: Path) -> list[str]:
    lp = cwd / ".autodev" / "plan-ledger.jsonl"
    if not lp.exists():
        return []
    out: list[str] = []
    for raw in lp.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        out.append(json.loads(line)["op"])
    return out


def _load_plan(cwd: Path) -> Plan:
    pm = PlanManager(cwd, session_id="sess-cli-rewind-readback")

    async def _load() -> Plan:
        plan = await pm.load()
        assert plan is not None
        return plan

    return asyncio.run(_load())


def _mk_two_phase_plan() -> Plan:
    """Phase 0 (one complete task, accepted) + phase 1 (one mid-flight
    task, in_progress phase review_status). Used for the happy-path
    and dry-run tests."""
    return Plan(
        plan_id="p-cli-rewind",
        spec_hash="0123456789abcdef",
        phases=[
            Phase(
                id="0",
                title="Phase 0",
                tasks=[
                    Task(
                        id="0.1",
                        phase_id="0",
                        title="t0.1",
                        description="d",
                        status="complete",
                    ),
                ],
            ),
            Phase(
                id="1",
                title="Phase 1",
                review_status="in_progress",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="t1.1",
                        description="d",
                        status="coded",
                        retry_count=2,
                    ),
                ],
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
    )


# ---------------------------------------------------------------------------
# Test 1 — --dry-run writes zero ledger entries.
# ---------------------------------------------------------------------------


def test_rewind_dry_run_no_mutation(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)
        _seed_plan(cwd, _mk_two_phase_plan())
        _seed_real_acceptance(cwd, "0")

        ops_before = _read_ledger_ops(cwd)

        result = runner.invoke(
            cli, ["rewind", "--to-phase", "0", "--dry-run"]
        )
        assert result.exit_code == 0, result.output
        assert "dry-run" in result.output.lower()

        ops_after = _read_ledger_ops(cwd)
        assert ops_after == ops_before, (
            "dry-run must not write ledger entries"
        )

        # Plan state untouched.
        plan = _load_plan(cwd)
        by_id = {t.id: t for phase in plan.phases for t in phase.tasks}
        assert by_id["1.1"].status == "coded"
        assert by_id["1.1"].retry_count == 2


# ---------------------------------------------------------------------------
# Test 2 — no stable phase to rewind to → exit 1 with an actionable
# message.
# ---------------------------------------------------------------------------


def test_rewind_with_no_stable_phase_exits_1_with_helpful_message(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)
        # Seed a plan but DO NOT seed any phase_review_complete event;
        # the detector therefore returns None.
        _seed_plan(cwd, _mk_two_phase_plan())

        result = runner.invoke(cli, ["rewind", "--yes"])
        assert result.exit_code == 1, result.output
        assert "no genuinely-accepted phase" in result.output.lower()
        # Operator-facing hint must mention the override.
        assert "--to-phase" in result.output


# ---------------------------------------------------------------------------
# Test 3 — full happy path: auto-detect target + --yes flips later
# phase tasks to pending and writes the audit ``op=rewind`` entry.
# ---------------------------------------------------------------------------


def test_rewind_full_happy_path(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)
        _seed_plan(cwd, _mk_two_phase_plan())
        # Genuine acceptance for phase 0 → detector returns "0".
        _seed_real_acceptance(cwd, "0")

        result = runner.invoke(cli, ["rewind", "--yes"])
        assert result.exit_code == 0, result.output
        assert "reset" in result.output.lower()

        # Plan state: phase-1 task back to pending, retry_count zeroed.
        plan = _load_plan(cwd)
        by_id = {t.id: t for phase in plan.phases for t in phase.tasks}
        assert by_id["0.1"].status == "complete"  # untouched
        assert by_id["1.1"].status == "pending"
        assert by_id["1.1"].retry_count == 0

        # Phase 1 review_status cleared.
        by_phase = {p.id: p for p in plan.phases}
        assert by_phase["1"].review_status is None

        # Ledger carries the audit ``rewind`` op.
        ops = _read_ledger_ops(cwd)
        assert "rewind" in ops
