"""Tests for ``autodev requeue`` CLI command (v0.28.0 Bug 8).

The ``requeue`` command flips blocked tasks back to ``pending`` so the
operator can recover from infrastructure-class failures (auth refreshes,
network blips, transient gateway 4xx) without losing the surrounding
plan structure. It is the small foundation patch that v0.29-v0.30
build on (typed ``block_reason_class``, ``rewind`` / ``quarantined``).

Each test exercises one CLI flag combination through ``CliRunner`` so
the production exit-code contract (0 success, 1 user error, 2
unexpected) is enforced end-to-end alongside the underlying ledger
mutation invariants.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

from click.testing import CliRunner

from cli import cli
from config.defaults import default_config
from config.loader import save_config
from state.schemas import Phase, Plan, Task


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _write_config(cwd: Path) -> None:
    """Write a minimal valid config.json into ``<cwd>/.autodev/``."""
    cfg = default_config()
    cfg.platform = "claude_code"  # type: ignore[assignment]
    autodev_dir = cwd / ".autodev"
    autodev_dir.mkdir(parents=True, exist_ok=True)
    save_config(cfg, autodev_dir / "config.json")


def _seed_plan(
    cwd: Path,
    plan: Plan,
    *,
    session_id: str = "sess-test-requeue",
) -> None:
    """Persist ``plan`` to disk via PlanManager.init_plan inside an
    asyncio event loop. Mirrors the seeding pattern used by the
    orchestrator integration suite."""
    import asyncio

    from state.plan_manager import PlanManager

    pm = PlanManager(cwd, session_id=session_id)

    async def _init() -> None:
        await pm.init_plan(plan)

    asyncio.run(_init())


def _read_ledger_ops(cwd: Path) -> list[str]:
    """Return the ``op`` field of every ledger entry in order."""
    lp = cwd / ".autodev" / "plan-ledger.jsonl"
    if not lp.exists():
        return []
    ops: list[str] = []
    for raw in lp.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        ops.append(json.loads(line)["op"])
    return ops


def _load_plan(cwd: Path) -> Plan:
    """Reload plan via PlanManager — the source of truth after a
    mutating CLI command finishes."""
    import asyncio

    from state.plan_manager import PlanManager

    pm = PlanManager(cwd, session_id="sess-test-readback")

    async def _load() -> Plan:
        result = await pm.load()
        assert result is not None
        return result

    return asyncio.run(_load())


def _mk_blocked_plan(blocked_reason: str = "auth_failed: 403") -> Plan:
    """Single-phase plan with one blocked task, used as the minimal
    seed for the happy-path tests."""
    return Plan(
        plan_id="p-requeue",
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
                        blocked_reason=blocked_reason,
                        retry_count=3,
                        escalated=True,
                        files=["src/foo.py"],
                    ),
                ],
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
    )


# ---------------------------------------------------------------------------
# Test 1 — empty plan exits 0 with friendly message.
# ---------------------------------------------------------------------------


def test_requeue_no_blocked_tasks_exits_0(tmp_path: Path) -> None:
    """No blocked tasks anywhere → exit 0, "nothing to requeue." message."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)
        plan = Plan(
            plan_id="p-empty",
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
        _seed_plan(cwd, plan)

        result = runner.invoke(cli, ["requeue", "--all-blocked", "--yes"])

        assert result.exit_code == 0, result.output
        assert "nothing to requeue" in result.output.lower()


# ---------------------------------------------------------------------------
# Test 2 — explicit --task flips one blocked task to pending.
# ---------------------------------------------------------------------------


def test_requeue_explicit_task_flips_to_pending(tmp_path: Path) -> None:
    """``requeue --task 1.1 --yes`` flips status, zeroes counters, and
    leaves a ``requeue`` audit op + ``update_task_status`` op behind."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)
        _seed_plan(cwd, _mk_blocked_plan())

        result = runner.invoke(cli, ["requeue", "--task", "1.1", "--yes"])

        assert result.exit_code == 0, result.output
        plan = _load_plan(cwd)
        task = plan.phases[0].tasks[0]
        assert task.status == "pending"
        assert task.retry_count == 0
        assert task.escalated is False
        assert task.blocked_reason is None

        ops = _read_ledger_ops(cwd)
        # init_plan, snapshot (init), requeue, update_task_status, snapshot.
        assert "requeue" in ops
        assert ops.count("update_task_status") == 1


# ---------------------------------------------------------------------------
# Test 3 — --phase flips only blocked tasks; complete tasks untouched.
# ---------------------------------------------------------------------------


def test_requeue_phase_flips_all_blocked_in_phase(tmp_path: Path) -> None:
    """A phase mixing complete + blocked tasks: only the blocked ones
    flip back to pending; complete tasks are left as-is."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)
        plan = Plan(
            plan_id="p-mixed",
            spec_hash="0123456789abcdef",
            phases=[
                Phase(
                    id="1",
                    title="Setup",
                    tasks=[
                        Task(
                            id="1.1",
                            phase_id="1",
                            title="done",
                            description="d1",
                            status="complete",
                        ),
                        Task(
                            id="1.2",
                            phase_id="1",
                            title="stuck",
                            description="d2",
                            status="blocked",
                            blocked_reason="auth_failed: 403",
                            retry_count=2,
                        ),
                        Task(
                            id="1.3",
                            phase_id="1",
                            title="also stuck",
                            description="d3",
                            status="blocked",
                            blocked_reason="qa_gate_timeout",
                            retry_count=4,
                        ),
                    ],
                ),
            ],
            created_at=_iso(),
            updated_at=_iso(),
        )
        _seed_plan(cwd, plan)

        result = runner.invoke(cli, ["requeue", "--phase", "1", "--yes"])

        assert result.exit_code == 0, result.output
        reloaded = _load_plan(cwd)
        statuses = {t.id: t.status for t in reloaded.phases[0].tasks}
        assert statuses == {
            "1.1": "complete",
            "1.2": "pending",
            "1.3": "pending",
        }


# ---------------------------------------------------------------------------
# Test 4 — phase ``review_status="accepted"`` resets to ``None``.
# ---------------------------------------------------------------------------


def test_requeue_phase_resets_phase_review_status(tmp_path: Path) -> None:
    """Requeueing inside a phase whose review_status was already
    "accepted" must flip review_status back to None so the
    phase-review tournament re-fires on the next execute pass."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)
        plan = Plan(
            plan_id="p-accepted",
            spec_hash="0123456789abcdef",
            phases=[
                Phase(
                    id="1",
                    title="Setup",
                    review_status="accepted",
                    tasks=[
                        Task(
                            id="1.1",
                            phase_id="1",
                            title="stuck",
                            description="d1",
                            status="blocked",
                            blocked_reason="auth_failed: 403",
                        ),
                    ],
                ),
            ],
            created_at=_iso(),
            updated_at=_iso(),
        )
        _seed_plan(cwd, plan)

        result = runner.invoke(cli, ["requeue", "--task", "1.1", "--yes"])

        assert result.exit_code == 0, result.output
        reloaded = _load_plan(cwd)
        assert reloaded.phases[0].review_status is None


# ---------------------------------------------------------------------------
# Test 5 — --infrastructure filter only flips infra-class blocks.
# ---------------------------------------------------------------------------


def test_requeue_infrastructure_filter_matches_403_only(tmp_path: Path) -> None:
    """``--infrastructure`` only flips blocked tasks whose
    ``blocked_reason`` matches the keyword heuristic. Non-infra
    blocks (qa-gate timeout, judge rejection) stay blocked."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)
        plan = Plan(
            plan_id="p-mix",
            spec_hash="0123456789abcdef",
            phases=[
                Phase(
                    id="1",
                    title="Setup",
                    tasks=[
                        Task(
                            id="1.1",
                            phase_id="1",
                            title="auth-stuck",
                            description="infra",
                            status="blocked",
                            blocked_reason="guardrail_exceeded: 403",
                        ),
                        Task(
                            id="1.2",
                            phase_id="1",
                            title="qa-stuck",
                            description="non-infra",
                            status="blocked",
                            blocked_reason="qa_gate_timeout",
                        ),
                        Task(
                            id="1.3",
                            phase_id="1",
                            title="judge-stuck",
                            description="non-infra",
                            status="blocked",
                            blocked_reason="verdict: rejected",
                        ),
                    ],
                ),
            ],
            created_at=_iso(),
            updated_at=_iso(),
        )
        _seed_plan(cwd, plan)

        result = runner.invoke(cli, ["requeue", "--infrastructure", "--yes"])

        assert result.exit_code == 0, result.output
        reloaded = _load_plan(cwd)
        statuses = {t.id: t.status for t in reloaded.phases[0].tasks}
        assert statuses == {
            "1.1": "pending",
            "1.2": "blocked",
            "1.3": "blocked",
        }


# ---------------------------------------------------------------------------
# Test 6 — --dry-run writes zero post-init ledger entries.
# ---------------------------------------------------------------------------


def test_requeue_dry_run_no_mutation(tmp_path: Path) -> None:
    """``--dry-run`` shows the planned actions but writes zero new
    ledger entries. The task remains ``blocked`` after the call."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)
        _seed_plan(cwd, _mk_blocked_plan())

        baseline_ops = _read_ledger_ops(cwd)
        result = runner.invoke(
            cli, ["requeue", "--task", "1.1", "--yes", "--dry-run"]
        )

        assert result.exit_code == 0, result.output
        post_ops = _read_ledger_ops(cwd)
        assert post_ops == baseline_ops
        reloaded = _load_plan(cwd)
        assert reloaded.phases[0].tasks[0].status == "blocked"


# ---------------------------------------------------------------------------
# Test 7 — second requeue call is a true no-op (no new ledger entries).
# ---------------------------------------------------------------------------


def test_requeue_idempotent(tmp_path: Path) -> None:
    """A second ``requeue`` call against the same already-pending task
    writes zero new ledger entries — same plan state in, same ledger
    state out."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)
        _seed_plan(cwd, _mk_blocked_plan())

        first = runner.invoke(cli, ["requeue", "--task", "1.1", "--yes"])
        assert first.exit_code == 0, first.output
        ops_after_first = _read_ledger_ops(cwd)

        second = runner.invoke(cli, ["requeue", "--task", "1.1", "--yes"])
        assert second.exit_code == 0, second.output
        ops_after_second = _read_ledger_ops(cwd)

        assert ops_after_second == ops_after_first
        assert "nothing to requeue" in second.output.lower()


# ---------------------------------------------------------------------------
# Test 8 — typo in --task FOO-99 exits 1 with a helpful error.
# ---------------------------------------------------------------------------


def test_requeue_unknown_task_id_exits_1(tmp_path: Path) -> None:
    """An unknown ``--task`` id is a user error: exit 1, plan
    untouched, message names the bad id and points at ``status``."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd)
        _seed_plan(cwd, _mk_blocked_plan())
        baseline_ops = _read_ledger_ops(cwd)

        result = runner.invoke(cli, ["requeue", "--task", "FOO-99", "--yes"])

        assert result.exit_code == 1
        assert "FOO-99" in result.output
        assert "unknown task id" in result.output.lower()
        # Plan unchanged.
        assert _read_ledger_ops(cwd) == baseline_ops
        reloaded = _load_plan(cwd)
        assert reloaded.phases[0].tasks[0].status == "blocked"
