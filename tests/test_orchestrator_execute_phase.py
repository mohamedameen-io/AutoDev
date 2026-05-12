"""Tests for :mod:`src.orchestrator.execute_phase`."""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from adapters.types import AgentResult
from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from state.plan_manager import PlanManager
from state.schemas import (
    AcceptanceCriterion,
    Phase,
    Plan,
    Task,
)

from stub_adapter import StubAdapter, ok, fail


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan(*, two_tasks: bool = False) -> Plan:
    tasks = [
        Task(
            id="1.1",
            phase_id="1",
            title="Add subtract",
            description="Implement subtract(a, b)",
            files=["math.py"],
            acceptance=[
                AcceptanceCriterion(id="ac-1", description="tests pass"),
            ],
        ),
    ]
    if two_tasks:
        tasks.append(
            Task(
                id="1.2",
                phase_id="1",
                title="Add divide",
                description="Implement divide(a, b)",
                files=["math.py"],
                acceptance=[
                    AcceptanceCriterion(id="ac-1", description="tests pass"),
                ],
            )
        )
    return Plan(
        plan_id="p-exec",
        spec_hash="d",
        phases=[Phase(id="1", title="Work", tasks=tasks)],
        created_at=_iso(),
        updated_at=_iso(),
    )


async def _make_orch_with_plan(
    cwd: Path, adapter: StubAdapter, *, two_tasks: bool = False
) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    # qa_retry_limit=3 by default is the retry-then-escalate threshold.
    registry = build_registry(cfg)
    orch = Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-exec",
    )
    await orch.plan_manager.init_plan(_mk_plan(two_tasks=two_tasks))
    return orch


def _coder_ok_with_diff() -> AgentResult:
    return AgentResult(
        success=True,
        text="wrote subtract",
        diff=(
            "diff --git a/math.py b/math.py\n"
            "--- a/math.py\n"
            "+++ b/math.py\n"
            "@@ -0,0 +1 @@\n"
            "+def subtract(a,b): return a-b\n"
        ),
        files_changed=[Path("math.py")],
        duration_s=0.1,
    )


def _reviewer(verdict: str) -> AgentResult:
    if verdict == "APPROVED":
        text = "APPROVED\n- clean"
    elif verdict == "NEEDS_CHANGES":
        text = "NEEDS_CHANGES\n- add a docstring"
    else:
        text = "REJECTED\n- completely wrong"
    return ok(text)


def _test_engineer_ok() -> AgentResult:
    return ok("ran pytest\nRESULTS: passed=3 failed=0 total=3")


def _test_engineer_fail() -> AgentResult:
    return ok("RESULTS: passed=0 failed=3 total=3\nAssertionError...")


@pytest.mark.asyncio
async def test_execute_happy_path(tmp_path: Path) -> None:
    adapter = StubAdapter(
        {
            "developer": _coder_ok_with_diff(),
            "reviewer": _reviewer("APPROVED"),
            "test_engineer": _test_engineer_ok(),
        }
    )
    orch = await _make_orch_with_plan(tmp_path, adapter)
    tasks = await orch.execute()
    assert len(tasks) == 1
    assert tasks[0].id == "1.1"
    assert tasks[0].status == "complete"
    # Evidence bundles written.
    evdir = tmp_path / ".autodev" / "evidence"
    assert (evdir / "1.1-developer.json").exists()
    assert (evdir / "1.1-review.json").exists()
    assert (evdir / "1.1-test.json").exists()
    assert (evdir / "1.1.patch").exists()


@pytest.mark.asyncio
async def test_execute_reviewer_needs_changes_retries(tmp_path: Path) -> None:
    adapter = StubAdapter(
        {
            "developer": [_coder_ok_with_diff(), _coder_ok_with_diff()],
            "reviewer": [_reviewer("NEEDS_CHANGES"), _reviewer("APPROVED")],
            "test_engineer": _test_engineer_ok(),
        }
    )
    orch = await _make_orch_with_plan(tmp_path, adapter)
    tasks = await orch.execute()
    assert tasks[0].status == "complete"
    assert tasks[0].retry_count == 1
    # Coder called twice (initial + retry), reviewer twice, test_engineer once.
    assert adapter.count("developer") == 2
    assert adapter.count("reviewer") == 2
    assert adapter.count("test_engineer") == 1


@pytest.mark.asyncio
async def test_execute_test_failure_retries_then_passes(tmp_path: Path) -> None:
    adapter = StubAdapter(
        {
            "developer": [_coder_ok_with_diff(), _coder_ok_with_diff()],
            "reviewer": _reviewer("APPROVED"),
            "test_engineer": [_test_engineer_fail(), _test_engineer_ok()],
        }
    )
    orch = await _make_orch_with_plan(tmp_path, adapter)
    tasks = await orch.execute()
    assert tasks[0].status == "complete"
    assert tasks[0].retry_count == 1


@pytest.mark.asyncio
async def test_execute_retry_exhaustion_escalates(tmp_path: Path) -> None:
    """After ``qa_retry_limit`` (default 3) failures, the v0.15.0 stuck
    ladder fires REFINE then PIVOT then SOFT_BLOCKER. The critic stub
    returns ``RESOLUTION: soft-blocker`` so the task is marked
    escalated + blocked the first time the ladder dispatches.
    """
    adapter = StubAdapter(
        {
            "developer": _coder_ok_with_diff(),
            "reviewer": _reviewer("NEEDS_CHANGES"),  # always fails
            "test_engineer": _test_engineer_ok(),
            "critic_sounding_board": ok(
                "diagnosis: planning gap\n\nRESOLUTION: soft-blocker\n"
            ),
        }
    )
    orch = await _make_orch_with_plan(tmp_path, adapter)
    tasks = await orch.execute()
    assert len(tasks) == 1
    task = tasks[0]
    assert task.escalated is True
    assert task.status == "blocked"
    # The ladder fired at the first threshold (REFINE at discard 3); the
    # critic returned soft-blocker, the task is blocked. Critic was
    # invoked exactly once.
    assert adapter.count("critic_sounding_board") == 1


@pytest.mark.asyncio
async def test_execute_coder_adapter_failure_retries_and_escalates(
    tmp_path: Path,
) -> None:
    adapter = StubAdapter(
        {
            "developer": fail("claude binary not found"),
            "reviewer": _reviewer("APPROVED"),
            "test_engineer": _test_engineer_ok(),
            "critic_sounding_board": ok(
                "cannot run coder\n\nRESOLUTION: soft-blocker\n"
            ),
        }
    )
    orch = await _make_orch_with_plan(tmp_path, adapter)
    tasks = await orch.execute()
    assert tasks[0].escalated is True
    assert tasks[0].status == "blocked"


@pytest.mark.asyncio
async def test_execute_multiple_tasks_sequence(tmp_path: Path) -> None:
    adapter = StubAdapter(
        {
            "developer": _coder_ok_with_diff(),
            "reviewer": _reviewer("APPROVED"),
            "test_engineer": _test_engineer_ok(),
        }
    )
    orch = await _make_orch_with_plan(tmp_path, adapter, two_tasks=True)
    tasks = await orch.execute()
    assert len(tasks) == 2
    assert all(t.status == "complete" for t in tasks)
    assert adapter.count("developer") == 2
    assert adapter.count("reviewer") == 2
    assert adapter.count("test_engineer") == 2


@pytest.mark.asyncio
async def test_execute_specific_task_id(tmp_path: Path) -> None:
    adapter = StubAdapter(
        {
            "developer": _coder_ok_with_diff(),
            "reviewer": _reviewer("APPROVED"),
            "test_engineer": _test_engineer_ok(),
        }
    )
    orch = await _make_orch_with_plan(tmp_path, adapter, two_tasks=True)
    tasks = await orch.execute(task_id="1.2")
    assert len(tasks) == 1
    assert tasks[0].id == "1.2"
    # Task 1.1 still pending.
    t11 = await orch.plan_manager.get_task("1.1")
    assert t11 is not None and t11.status == "pending"


@pytest.mark.asyncio
async def test_execute_unknown_task_id_raises(tmp_path: Path) -> None:
    from errors import AutodevError

    adapter = StubAdapter({})
    orch = await _make_orch_with_plan(tmp_path, adapter)
    with pytest.raises(AutodevError):
        await orch.execute(task_id="bogus")


@pytest.mark.asyncio
async def test_status_reports_counts(tmp_path: Path) -> None:
    adapter = StubAdapter(
        {
            "developer": _coder_ok_with_diff(),
            "reviewer": _reviewer("APPROVED"),
            "test_engineer": _test_engineer_ok(),
        }
    )
    orch = await _make_orch_with_plan(tmp_path, adapter, two_tasks=True)
    await orch.execute(task_id="1.1")
    snap = await orch.status()
    assert snap["plan"] is not None
    assert snap["totals"]["complete"] == 1
    assert snap["totals"]["pending"] == 1
    assert snap["totals"]["total"] == 2


@pytest.mark.asyncio
async def test_execute_phase_skips_tasks_with_requires(tmp_path: Path) -> None:
    """A task whose ``requires`` is non-empty must be marked ``skipped`` —
    the developer adapter must NEVER be invoked for it — and surrounding
    tasks must execute normally.
    """
    # Custom plan: task 1.1 marked as hardware-required; task 1.2 is normal.
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    registry = build_registry(cfg)

    adapter = StubAdapter(
        {
            "developer": _coder_ok_with_diff(),
            "reviewer": _reviewer("APPROVED"),
            "test_engineer": _test_engineer_ok(),
        }
    )
    orch = Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-requires-skip",
    )
    plan = Plan(
        plan_id="p-requires",
        spec_hash="d",
        phases=[
            Phase(
                id="1",
                title="Work",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="Flash firmware",
                        description="Hardware step",
                        files=["firmware.bin"],
                        acceptance=[
                            AcceptanceCriterion(
                                id="ac-1", description="device boots"
                            ),
                        ],
                        requires=["hardware"],
                    ),
                    Task(
                        id="1.2",
                        phase_id="1",
                        title="Add subtract",
                        description="Implement subtract(a, b)",
                        files=["math.py"],
                        acceptance=[
                            AcceptanceCriterion(
                                id="ac-1", description="tests pass"
                            ),
                        ],
                    ),
                ],
            )
        ],
        created_at=_iso(),
        updated_at=_iso(),
    )
    await orch.plan_manager.init_plan(plan)

    tasks = await orch.execute()

    # Both tasks are reported as processed.
    assert len(tasks) == 2
    by_id = {t.id: t for t in tasks}

    # Task 1.1 — skipped, never reached the developer.
    skipped = by_id["1.1"]
    assert skipped.status == "skipped"
    assert skipped.blocked_reason is not None
    assert "hardware" in skipped.blocked_reason
    assert "requires=" in skipped.blocked_reason

    # Task 1.2 — completed normally.
    done = by_id["1.2"]
    assert done.status == "complete"

    # The developer adapter ran exactly once (for 1.2 only).
    assert adapter.count("developer") == 1
    assert adapter.count("reviewer") == 1
    assert adapter.count("test_engineer") == 1


@pytest.mark.asyncio
async def test_execute_phase_skips_specific_task_with_requires(tmp_path: Path) -> None:
    """When ``task_id`` targets a requires-marked task directly, it is still
    skipped (not executed) — the explicit-target path must respect the field.
    """
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    registry = build_registry(cfg)

    adapter = StubAdapter(
        {
            "developer": _coder_ok_with_diff(),
            "reviewer": _reviewer("APPROVED"),
            "test_engineer": _test_engineer_ok(),
        }
    )
    orch = Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-requires-target",
    )
    plan = Plan(
        plan_id="p-requires-target",
        spec_hash="d",
        phases=[
            Phase(
                id="1",
                title="Work",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="Hand off to operator",
                        description="Manual step",
                        files=[],
                        acceptance=[],
                        requires=["human"],
                    ),
                ],
            )
        ],
        created_at=_iso(),
        updated_at=_iso(),
    )
    await orch.plan_manager.init_plan(plan)

    tasks = await orch.execute(task_id="1.1")

    assert len(tasks) == 1
    assert tasks[0].status == "skipped"
    assert "human" in (tasks[0].blocked_reason or "")
    # No agent of any kind was invoked.
    assert adapter.count("developer") == 0
    assert adapter.count("reviewer") == 0
    assert adapter.count("test_engineer") == 0


@pytest.mark.asyncio
async def test_resume_picks_up_pending_tasks(tmp_path: Path) -> None:
    adapter = StubAdapter(
        {
            "developer": _coder_ok_with_diff(),
            "reviewer": _reviewer("APPROVED"),
            "test_engineer": _test_engineer_ok(),
        }
    )
    orch = await _make_orch_with_plan(tmp_path, adapter, two_tasks=True)
    await orch.execute(task_id="1.1")  # finish task 1 only
    # Fresh orchestrator / adapter for resume.
    adapter2 = StubAdapter(
        {
            "developer": _coder_ok_with_diff(),
            "reviewer": _reviewer("APPROVED"),
            "test_engineer": _test_engineer_ok(),
        }
    )
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    orch2 = Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=adapter2,
        registry=build_registry(cfg),
        session_id="sess-resume",
    )
    tasks = await orch2.resume()
    assert [t.id for t in tasks] == ["1.2"]
    pm = PlanManager(tmp_path, session_id="reader")
    final = await pm.load()
    assert final is not None
    assert all(t.status == "complete" for p in final.phases for t in p.tasks)


# ---------------------------------------------------------------------------
# v0.8.0 — per-task complexity → AgentInvocation max_turns + timeout_s
# ---------------------------------------------------------------------------


def _mk_plan_with_complexity(complexity: str | None) -> Plan:
    """Plan with a single task carrying the given complexity bucket."""
    return Plan(
        plan_id="p-complexity",
        spec_hash="d",
        phases=[
            Phase(
                id="1",
                title="Work",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="t",
                        description="d",
                        files=["math.py"],
                        complexity=complexity,  # type: ignore[arg-type]
                        acceptance=[
                            AcceptanceCriterion(id="ac-1", description="passes"),
                        ],
                    ),
                ],
            )
        ],
        created_at=_iso(),
        updated_at=_iso(),
    )


async def _orch_with_complexity_plan(
    cwd: Path, adapter: StubAdapter, complexity: str | None
) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    registry = build_registry(cfg)
    orch = Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-complexity",
    )
    await orch.plan_manager.init_plan(_mk_plan_with_complexity(complexity))
    return orch


def _developer_invocation(adapter: StubAdapter):
    """Return the first AgentInvocation the stub recorded for the developer."""
    devs = [c for c in adapter.calls if c.role == "developer"]
    assert devs, "expected at least one developer invocation"
    return devs[0]


@pytest.mark.asyncio
async def test_developer_max_turns_overridden_by_task_complexity_complex(
    tmp_path: Path,
) -> None:
    """``Task(complexity="complex")`` causes the developer's invocation to
    run with ``max_turns=40`` and ``timeout_s=1800`` — the per-task scaling
    that v0.8.0 introduces. Anchors the load-bearing edit at
    ``execute_phase.delegate``.
    """
    adapter = StubAdapter(
        {
            "developer": _coder_ok_with_diff(),
            "reviewer": _reviewer("APPROVED"),
            "test_engineer": _test_engineer_ok(),
        }
    )
    orch = await _orch_with_complexity_plan(tmp_path, adapter, "complex")
    await orch.execute()
    inv = _developer_invocation(adapter)
    assert inv.max_turns == 40
    assert inv.timeout_s == 1800


@pytest.mark.asyncio
async def test_developer_timeout_overridden_by_task_complexity_simple(
    tmp_path: Path,
) -> None:
    """``Task(complexity="simple")`` → developer ``timeout_s == 600`` (the
    cheapest tier; no need to burn a 30-min wall clock on a one-file diff).
    """
    adapter = StubAdapter(
        {
            "developer": _coder_ok_with_diff(),
            "reviewer": _reviewer("APPROVED"),
            "test_engineer": _test_engineer_ok(),
        }
    )
    orch = await _orch_with_complexity_plan(tmp_path, adapter, "simple")
    await orch.execute()
    inv = _developer_invocation(adapter)
    assert inv.max_turns == 10
    assert inv.timeout_s == 600


@pytest.mark.asyncio
async def test_developer_max_turns_falls_back_to_spec_when_complexity_none(
    tmp_path: Path,
) -> None:
    """Regression — v0.7.0 behavior preserved when ``Task.complexity is None``.
    The developer's invocation uses ``spec.max_turns`` (10 in defaults) and
    the orchestrator-level fallback timeout (``_DEFAULT_DEVELOPER_TIMEOUT_S
    = 900``).
    """
    adapter = StubAdapter(
        {
            "developer": _coder_ok_with_diff(),
            "reviewer": _reviewer("APPROVED"),
            "test_engineer": _test_engineer_ok(),
        }
    )
    orch = await _orch_with_complexity_plan(tmp_path, adapter, None)
    await orch.execute()
    inv = _developer_invocation(adapter)
    # spec.max_turns for the developer is 10 in default_config().
    assert inv.max_turns == 10
    # Orchestrator fallback when no per-task override: 900s.
    assert inv.timeout_s == 900


def test_developer_envelope_context_contains_complexity_hint() -> None:
    """v0.8.0 polish: the developer's envelope context surfaces the task
    complexity so the prompt itself reinforces the per-task budget. A task
    with ``complexity=None`` defaults to the prompt-side ``"medium"``
    fallback (matching the orchestrator's spec-default shape)."""
    from orchestrator.execute_phase import _developer_envelope

    complex_task = Task(
        id="1.1",
        phase_id="1",
        title="big refactor",
        description="d",
        complexity="complex",
    )
    env = _developer_envelope(complex_task, extra_issues=[])
    assert env.context["complexity"] == "complex"

    untagged_task = Task(
        id="1.2",
        phase_id="1",
        title="legacy",
        description="d",
    )
    env_legacy = _developer_envelope(untagged_task, extra_issues=[])
    assert env_legacy.context["complexity"] == "medium"


# ---------------------------------------------------------------------------
# v0.26.1 patch E: worker exception classification + traceback persistence.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_unicode_decode_error_classified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``_execute_one`` raises ``UnicodeDecodeError``, the worker
    classifies it as ``qa_gate_encoding_error`` in ``blocked_reason``.
    """
    from orchestrator import execute_phase as ep

    async def boom(*_a: object, **_kw: object) -> object:
        raise UnicodeDecodeError("utf-8", b"\xe8", 0, 1, "invalid start byte")

    monkeypatch.setattr(ep, "_execute_one", boom)

    adapter = StubAdapter(responses={})
    orch = await _make_orch_with_plan(tmp_path, adapter)
    task = await orch.plan_manager.get_task("1.1")
    assert task is not None

    out = await ep._execute_one_worker(orch, task, worktree_mgr=None)

    assert out.status == "blocked"
    reason = out.blocked_reason or ""
    assert reason.startswith("qa_gate_encoding_error"), reason


@pytest.mark.asyncio
async def test_worker_os_error_classified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``OSError`` (file IO) maps to ``qa_gate_io_error``."""
    from orchestrator import execute_phase as ep

    async def boom(*_a: object, **_kw: object) -> object:
        raise OSError("permission denied")

    monkeypatch.setattr(ep, "_execute_one", boom)

    adapter = StubAdapter(responses={})
    orch = await _make_orch_with_plan(tmp_path, adapter)
    task = await orch.plan_manager.get_task("1.1")
    assert task is not None

    out = await ep._execute_one_worker(orch, task, worktree_mgr=None)

    assert out.status == "blocked"
    reason = out.blocked_reason or ""
    assert reason.startswith("qa_gate_io_error"), reason


@pytest.mark.asyncio
async def test_worker_timeout_classified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``asyncio.TimeoutError`` (and ``TimeoutError``) map to
    ``qa_gate_timeout``.
    """
    import asyncio

    from orchestrator import execute_phase as ep

    async def boom(*_a: object, **_kw: object) -> object:
        raise asyncio.TimeoutError()

    monkeypatch.setattr(ep, "_execute_one", boom)

    adapter = StubAdapter(responses={})
    orch = await _make_orch_with_plan(tmp_path, adapter)
    task = await orch.plan_manager.get_task("1.1")
    assert task is not None

    out = await ep._execute_one_worker(orch, task, worktree_mgr=None)

    assert out.status == "blocked"
    reason = out.blocked_reason or ""
    assert reason.startswith("qa_gate_timeout"), reason


@pytest.mark.asyncio
async def test_worker_generic_exception_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unrecognised exception types keep the legacy ``worker_exception:``
    prefix — back-compat with v0.25.* operators grepping log lines."""
    from orchestrator import execute_phase as ep

    class WeirdError(Exception):
        pass

    async def boom(*_a: object, **_kw: object) -> object:
        raise WeirdError("something unusual")

    monkeypatch.setattr(ep, "_execute_one", boom)

    adapter = StubAdapter(responses={})
    orch = await _make_orch_with_plan(tmp_path, adapter)
    task = await orch.plan_manager.get_task("1.1")
    assert task is not None

    out = await ep._execute_one_worker(orch, task, worktree_mgr=None)

    assert out.status == "blocked"
    reason = out.blocked_reason or ""
    assert reason.startswith("worker_exception"), reason


@pytest.mark.asyncio
async def test_worker_exception_writes_traceback_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Patch E: traceback is persisted to ``.autodev/debug/`` so operators
    can diagnose without re-running with verbose logging."""
    from orchestrator import execute_phase as ep
    from state.paths import debug_dir

    async def boom(*_a: object, **_kw: object) -> object:
        raise UnicodeDecodeError("utf-8", b"\xe8", 0, 1, "trace it")

    monkeypatch.setattr(ep, "_execute_one", boom)

    adapter = StubAdapter(responses={})
    orch = await _make_orch_with_plan(tmp_path, adapter)
    task = await orch.plan_manager.get_task("1.1")
    assert task is not None

    await ep._execute_one_worker(orch, task, worktree_mgr=None)

    dbg = debug_dir(tmp_path)
    assert dbg.exists()
    files = sorted(dbg.glob("worker-exception-1.1-*.txt"))
    assert files, f"no worker-exception-1.1 traceback in {dbg}"
    content = files[0].read_text(encoding="utf-8", errors="replace")
    # Traceback should reference the exception type name.
    assert "UnicodeDecodeError" in content
