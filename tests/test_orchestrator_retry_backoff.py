"""v0.25.1 Bug #4 — retry backoff persistence across resume.

Regression: on ``autodev resume``, the orchestrator preserved
``retry_count`` from the prior session (correct) but the dispatch loop
had zero interval enforcement between attempts. A task wedged at
``retry_count=N`` would burn through retries N+1, N+2, … within
milliseconds (sub-second ledger sequences observed in the unity run),
blowing through ``qa_retry_limit`` before any state could be reassessed.

Fix: persist ``last_retry_at`` on the :class:`Task` model and enforce a
minimum interval (``qa_retry_min_interval_s``, default ``30.0`` s) before
the next retry dispatches.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from state.plan_manager import PlanManager
from state.schemas import Phase, Plan, Task


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan() -> Plan:
    return Plan(
        plan_id="p-test",
        spec_hash="cafebabe",
        phases=[
            Phase(
                id="1",
                title="Setup",
                tasks=[
                    Task(id="1.1", phase_id="1", title="t", description="d"),
                ],
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
    )


def test_task_schema_has_last_retry_at_field() -> None:
    """``last_retry_at`` defaults to ``None`` for backward compat with
    pre-v0.25.1 ledgers (older plans don't carry the field)."""
    t = Task(id="1.1", phase_id="1", title="t", description="d")
    assert t.last_retry_at is None


@pytest.mark.asyncio
async def test_mark_task_retry_sets_last_retry_at(tmp_path: Path) -> None:
    """``mark_task_retry`` must stamp ``task.last_retry_at`` so the
    next dispatch can enforce a minimum interval."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    await pm.update_task_status("1.1", "in_progress")

    before = _dt.datetime.now(_dt.timezone.utc)
    await pm.mark_task_retry("1.1")
    after = _dt.datetime.now(_dt.timezone.utc)

    t = await pm.get_task("1.1")
    assert t is not None
    assert t.last_retry_at is not None
    stamped = _dt.datetime.fromisoformat(t.last_retry_at)
    # Strip microsecond tolerance with a generous bound.
    assert before - _dt.timedelta(seconds=1) <= stamped <= after + _dt.timedelta(
        seconds=1
    )


@pytest.mark.asyncio
async def test_ledger_replay_restores_last_retry_at(tmp_path: Path) -> None:
    """A new :class:`PlanManager` replaying the ledger must restore
    ``last_retry_at`` from the recorded ``update_task_status`` op so
    backoff enforcement survives ``autodev resume``."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(_mk_plan())
    await pm.update_task_status("1.1", "in_progress")
    await pm.mark_task_retry("1.1")
    t_before = await pm.get_task("1.1")
    assert t_before is not None and t_before.last_retry_at is not None

    # Simulate a fresh session reading the ledger.
    pm2 = PlanManager(tmp_path, session_id="s2")
    t_after = await pm2.get_task("1.1")
    assert t_after is not None
    assert t_after.last_retry_at == t_before.last_retry_at
    assert t_after.retry_count == 1


@pytest.mark.asyncio
async def test_enforce_retry_backoff_sleeps_when_recent() -> None:
    """``_enforce_retry_backoff`` must wait when ``last_retry_at`` is
    inside the configured interval. Uses injected ``now`` and ``sleep``
    so the test runs instantly without wall-clock waits."""
    from orchestrator.execute_phase import _enforce_retry_backoff

    fixed_now = _dt.datetime(2026, 5, 11, 12, 0, 0, tzinfo=_dt.timezone.utc)
    last_retry_at = (fixed_now - _dt.timedelta(seconds=5)).isoformat()
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    waited = await _enforce_retry_backoff(
        last_retry_at,
        min_interval_s=30.0,
        now=lambda: fixed_now,
        sleep=fake_sleep,
    )

    # Should wait 30 - 5 = 25 seconds.
    assert sleeps == [25.0]
    assert waited == 25.0


@pytest.mark.asyncio
async def test_enforce_retry_backoff_skips_when_old() -> None:
    """When ``last_retry_at`` is older than the interval, no sleep."""
    from orchestrator.execute_phase import _enforce_retry_backoff

    fixed_now = _dt.datetime(2026, 5, 11, 12, 0, 0, tzinfo=_dt.timezone.utc)
    last_retry_at = (fixed_now - _dt.timedelta(seconds=60)).isoformat()
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    waited = await _enforce_retry_backoff(
        last_retry_at,
        min_interval_s=30.0,
        now=lambda: fixed_now,
        sleep=fake_sleep,
    )

    assert sleeps == []
    assert waited == 0.0


@pytest.mark.asyncio
async def test_enforce_retry_backoff_skips_when_none() -> None:
    """First retry of a task (``last_retry_at is None``) must not sleep."""
    from orchestrator.execute_phase import _enforce_retry_backoff

    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    waited = await _enforce_retry_backoff(
        None,
        min_interval_s=30.0,
        sleep=fake_sleep,
    )

    assert sleeps == []
    assert waited == 0.0


@pytest.mark.asyncio
async def test_enforce_retry_backoff_skips_when_interval_zero() -> None:
    """``qa_retry_min_interval_s=0`` disables the guard entirely."""
    from orchestrator.execute_phase import _enforce_retry_backoff

    fixed_now = _dt.datetime(2026, 5, 11, 12, 0, 0, tzinfo=_dt.timezone.utc)
    last_retry_at = (fixed_now - _dt.timedelta(seconds=1)).isoformat()
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    waited = await _enforce_retry_backoff(
        last_retry_at,
        min_interval_s=0.0,
        now=lambda: fixed_now,
        sleep=fake_sleep,
    )

    assert sleeps == []
    assert waited == 0.0


def test_config_has_qa_retry_min_interval_s_default() -> None:
    """``qa_retry_min_interval_s`` defaults to ``30.0`` s on
    :class:`AutodevConfig`."""
    from config.schema import AutodevConfig

    fields = AutodevConfig.model_fields
    assert "qa_retry_min_interval_s" in fields
    assert fields["qa_retry_min_interval_s"].default == 30.0
