"""v0.25.4 — typed error for InlineAdapter + tournaments mismatch.

InlineAdapter is single-process by construction (its ``parallel()``
raises ``NotImplementedError``). Tournaments fan out IAG-isolated
branches and judges in parallel via ``adapter.parallel()``. The two
were guarded by three bare ``assert`` statements inside each tournament
runner — fine for catching the bug in dev, useless for a release
build (no operator guidance, fires deep in the call stack).

v0.25.4 raises a typed :class:`TournamentAdapterMismatchError`
(subclass of :class:`ConfigError`) at the **entry** of each
tournament-firing flow, before any LLM call. The runner-level asserts
are promoted to explicit raises of the same exception for defense in
depth (so the failure survives ``python -O``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from adapters.inline import InlineAdapter
from agents import build_registry
from config.defaults import default_config
from errors import ConfigError, TournamentAdapterMismatchError
from orchestrator import Orchestrator
from orchestrator.preflight import check_tournament_adapter_compatibility

from stub_adapter import StubAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _orch_with_inline(
    cwd: Path,
    *,
    plan_enabled: bool = True,
    impl_enabled: bool = True,
    phase_review_enabled: bool = True,
) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = plan_enabled
    cfg.tournaments.impl.enabled = impl_enabled
    cfg.tournaments.phase_review.enabled = phase_review_enabled
    registry = build_registry(cfg)
    return Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=InlineAdapter(cwd=cwd),
        registry=registry,
        session_id="sess-test-mismatch",
    )


def _orch_with_subprocess(
    cwd: Path,
    *,
    plan_enabled: bool = True,
    impl_enabled: bool = True,
    phase_review_enabled: bool = True,
) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = plan_enabled
    cfg.tournaments.impl.enabled = impl_enabled
    cfg.tournaments.phase_review.enabled = phase_review_enabled
    registry = build_registry(cfg)
    return Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=StubAdapter({}),
        registry=registry,
        session_id="sess-test-subprocess",
    )


# ---------------------------------------------------------------------------
# TournamentAdapterMismatchError shape
# ---------------------------------------------------------------------------


def test_error_is_subclass_of_config_error() -> None:
    """Operators catching ``ConfigError`` (a typed bucket they already
    handle) should automatically catch the new mismatch error too."""
    assert issubclass(TournamentAdapterMismatchError, ConfigError)


def test_error_message_lists_enabled_phases() -> None:
    err = TournamentAdapterMismatchError(["plan", "impl"])
    msg = str(err)
    assert "plan" in msg
    assert "impl" in msg
    # The message must point operators to the two actionable fixes.
    assert "platform: claude_code" in msg or "platform: 'claude_code'" in msg or "platform: `claude_code`" in msg
    assert "tournaments" in msg.lower()


def test_error_records_enabled_phases() -> None:
    err = TournamentAdapterMismatchError(["plan", "impl", "phase_review"])
    assert err.enabled_phases == ["plan", "impl", "phase_review"]


# ---------------------------------------------------------------------------
# Preflight check — InlineAdapter + each tournament type
# ---------------------------------------------------------------------------


def test_inline_plus_plan_tournament_raises_preflight(tmp_path: Path) -> None:
    """InlineAdapter + plan.enabled=True → preflight raises with
    ``["plan"]`` in the message."""
    orch = _orch_with_inline(
        tmp_path,
        plan_enabled=True,
        impl_enabled=False,
        phase_review_enabled=False,
    )
    with pytest.raises(TournamentAdapterMismatchError) as exc_info:
        check_tournament_adapter_compatibility(orch)
    assert "plan" in exc_info.value.enabled_phases
    assert "impl" not in exc_info.value.enabled_phases
    assert "phase_review" not in exc_info.value.enabled_phases


def test_inline_plus_impl_tournament_raises_preflight(tmp_path: Path) -> None:
    orch = _orch_with_inline(
        tmp_path,
        plan_enabled=False,
        impl_enabled=True,
        phase_review_enabled=False,
    )
    with pytest.raises(TournamentAdapterMismatchError) as exc_info:
        check_tournament_adapter_compatibility(orch)
    assert exc_info.value.enabled_phases == ["impl"]


def test_inline_plus_phase_review_raises_preflight(tmp_path: Path) -> None:
    orch = _orch_with_inline(
        tmp_path,
        plan_enabled=False,
        impl_enabled=False,
        phase_review_enabled=True,
    )
    with pytest.raises(TournamentAdapterMismatchError) as exc_info:
        check_tournament_adapter_compatibility(orch)
    assert exc_info.value.enabled_phases == ["phase_review"]


def test_inline_plus_multiple_tournaments_lists_all(tmp_path: Path) -> None:
    """All three tournaments enabled → message lists all three labels."""
    orch = _orch_with_inline(
        tmp_path,
        plan_enabled=True,
        impl_enabled=True,
        phase_review_enabled=True,
    )
    with pytest.raises(TournamentAdapterMismatchError) as exc_info:
        check_tournament_adapter_compatibility(orch)
    assert set(exc_info.value.enabled_phases) == {"plan", "impl", "phase_review"}
    msg = str(exc_info.value)
    for label in ("plan", "impl", "phase_review"):
        assert label in msg


# ---------------------------------------------------------------------------
# Negative path: preflight is a noop when adapter is fine OR tournaments off
# ---------------------------------------------------------------------------


def test_subprocess_adapter_no_raise(tmp_path: Path) -> None:
    """``StubAdapter`` (proxy for subprocess adapters) + tournaments on
    → preflight passes silently."""
    orch = _orch_with_subprocess(tmp_path)
    # Must not raise.
    check_tournament_adapter_compatibility(orch)


def test_inline_with_all_tournaments_disabled_no_raise(tmp_path: Path) -> None:
    """InlineAdapter is still legal when nobody plans to fan out. The
    legacy inline-only workflow (no tournaments) must keep working
    until v0.26.0 deletes InlineAdapter entirely."""
    orch = _orch_with_inline(
        tmp_path,
        plan_enabled=False,
        impl_enabled=False,
        phase_review_enabled=False,
    )
    check_tournament_adapter_compatibility(orch)


# ---------------------------------------------------------------------------
# Runner-level guard: defense-in-depth raise (survives ``python -O``)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_tournament_runner_raises_typed_error_for_inline(
    tmp_path: Path,
) -> None:
    """Even if a future caller bypasses ``check_tournament_adapter_compatibility``
    and gets all the way into ``run_plan_tournament``, the runner's own
    guard must raise the typed exception (not ``AssertionError``)."""
    from orchestrator.plan_tournament_runner import run_plan_tournament

    orch = _orch_with_inline(tmp_path)
    with pytest.raises(TournamentAdapterMismatchError):
        await run_plan_tournament(
            orch, "# initial plan\n", "noop intent", spec_hash="0123456789abcdef"
        )


@pytest.mark.asyncio
async def test_impl_tournament_runner_raises_typed_error_for_inline(
    tmp_path: Path,
) -> None:
    from orchestrator.impl_tournament_runner import run_impl_tournament
    from state.schemas import Phase, Plan, Task
    import datetime as _dt
    from tournament import ImplBundle

    orch = _orch_with_inline(tmp_path)
    plan = Plan(
        plan_id="p-test",
        spec_hash="x",
        phases=[
            Phase(
                id="1",
                title="Work",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="Add foo",
                        description="Add foo()",
                    )
                ],
            )
        ],
        created_at=_dt.datetime.now(_dt.timezone.utc).isoformat(),
        updated_at=_dt.datetime.now(_dt.timezone.utc).isoformat(),
    )
    await orch.plan_manager.init_plan(plan)
    task = await orch.plan_manager.get_task("1.1")
    assert task is not None
    initial = ImplBundle(
        task_id="1.1",
        task_description="Add foo()",
        diff="+def foo(): pass",
        files_changed=["foo.py"],
        tests_passed=1,
        tests_failed=0,
        tests_total=1,
        test_output_excerpt="1 passed",
    )
    with pytest.raises(TournamentAdapterMismatchError):
        await run_impl_tournament(orch, task, initial)
