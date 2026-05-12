"""v0.27 Phase 3 (audit §3): task-scoped edit_scope-violation block.

Pre-v0.27 the orchestrator blocked EVERY pending task in EVERY phase
on a single edit_scope violation. v0.27 narrows the block to just the
offending tasks; only when every pending task violates does the
blanket-block fire (preserves the v0.26.2 safety semantics for the
truly-broken plan case).

Tests:

  1. ``collect_edit_scope_violations`` returns one entry per offending
     task.
  2. A single-task violation blocks only that task; siblings remain
     pending.
  3. When every pending task violates, the blanket-block fires
     (back-compat for v0.26.2-style structural plan errors).
  4. ``validate_edit_scope`` (the back-compat wrapper) still raises
     the first violation.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator.dag import (
    EditScopeViolation,
    collect_edit_scope_violations,
    validate_edit_scope,
)
from state.schemas import (
    AcceptanceCriterion,
    Phase,
    Plan,
    Task,
)

from stub_adapter import StubAdapter, ok


def _make_plan(tasks_files: list[tuple[str, list[str]]]) -> Plan:
    """Build a minimal Plan with one phase whose tasks have the given
    (id, files) pairs. ``plan.edit_scope`` is fixed at ``src/math``."""
    tasks = []
    for task_id, files in tasks_files:
        tasks.append(
            Task(
                id=task_id,
                phase_id="1",
                title=f"task {task_id}",
                description=f"task {task_id}",
                files=files,
                acceptance=[
                    AcceptanceCriterion(id=f"ac-{task_id}", description="ok"),
                ],
                assigned_agent="developer",
            )
        )
    phase = Phase(id="1", title="impl", tasks=tasks)
    return Plan(
        plan_id="p-test",
        spec_hash="abc",
        phases=[phase],
        metadata={"title": "Test"},
        edit_scope=["src/math"],
        created_at="2026-05-12T00:00:00Z",
        updated_at="2026-05-12T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# collect_edit_scope_violations
# ---------------------------------------------------------------------------


def test_collect_returns_empty_when_all_tasks_in_scope() -> None:
    plan = _make_plan(
        [
            ("1.1", ["src/math/__init__.py"]),
            ("1.2", ["src/math/helpers.py"]),
        ]
    )
    assert collect_edit_scope_violations(plan) == []


def test_collect_returns_one_per_offending_task() -> None:
    """A plan with two violating tasks produces two entries."""
    plan = _make_plan(
        [
            ("1.1", ["src/math/__init__.py"]),
            ("1.2", ["src/cache/lru.py"]),  # outside scope
            ("1.3", ["src/dag.py"]),  # outside scope
        ]
    )
    violations = collect_edit_scope_violations(plan)
    assert len(violations) == 2
    offending_ids = {getattr(v, "task_id", None) for v in violations}
    assert offending_ids == {"1.2", "1.3"}


def test_collect_attaches_task_phase_file_metadata() -> None:
    plan = _make_plan([("1.1", ["src/cache/x.py"])])
    violations = collect_edit_scope_violations(plan)
    assert len(violations) == 1
    v = violations[0]
    assert getattr(v, "task_id") == "1.1"
    assert getattr(v, "phase_id") == "1"
    assert getattr(v, "file_path") == "src/cache/x.py"


# ---------------------------------------------------------------------------
# validate_edit_scope back-compat: still raises first violation.
# ---------------------------------------------------------------------------


def test_validate_edit_scope_raises_first_violation() -> None:
    plan = _make_plan(
        [
            ("1.1", ["src/math/__init__.py"]),
            ("1.2", ["src/cache/lru.py"]),
        ]
    )
    with pytest.raises(EditScopeViolation):
        validate_edit_scope(plan)


def test_validate_edit_scope_silent_when_no_violation() -> None:
    plan = _make_plan([("1.1", ["src/math/__init__.py"])])
    validate_edit_scope(plan)  # no raise


# ---------------------------------------------------------------------------
# execute_phase: task-scoped block vs. blanket-block.
# ---------------------------------------------------------------------------


def _init_repo(cwd: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(cwd), check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=str(cwd), check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=str(cwd), check=True
    )
    (cwd / "src" / "math").mkdir(parents=True, exist_ok=True)
    (cwd / "src" / "math" / "__init__.py").write_text(
        "def add(a, b): return a + b\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "-A"], cwd=str(cwd), check=True)
    subprocess.run(
        ["git", "commit", "-qm", "init"], cwd=str(cwd), check=True
    )


_PLAN_ONE_TASK_VIOLATES_OTHER_OK = """
# Plan: Demo

EDIT_SCOPE:
  - src/math

## Phase 1: Implement
  - Acceptance:
    - [ ] subtract function exported

### Task 1.1: real file
  - Description: this stays in scope
  - Files: src/math/__init__.py
  - Acceptance:
    - [ ] subtract function exported

### Task 1.2: violator
  - Description: declares a file outside the plan scope
  - Files: src/cache/lru.py
  - Acceptance:
    - [ ] cache lru implemented
"""


@pytest.mark.asyncio
async def test_only_violating_task_blocked_siblings_remain_pending(
    tmp_path: Path,
) -> None:
    """v0.27 behaviour: only the offending task is blocked. Siblings
    survive."""
    _init_repo(tmp_path)
    # Also create the cache dir so file_existence_validator passes; the
    # bug we want to surface is the edit_scope check, not missing-on-disk.
    (tmp_path / "src" / "cache").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "cache" / "lru.py").write_text(
        "# placeholder\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
    subprocess.run(
        ["git", "commit", "-qm", "cache placeholder"],
        cwd=str(tmp_path),
        check=True,
    )

    adapter = StubAdapter(
        {
            "explorer": ok("found"),
            "domain_expert": ok("ok"),
            "architect": ok(_PLAN_ONE_TASK_VIOLATES_OTHER_OK),
        }
    )
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    registry = build_registry(cfg)
    orch = Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-cascade",
    )

    plan = await orch.plan("Demo")
    assert plan is not None

    # The cascade-block fires BEFORE any task runs and returns the
    # empty processed list, so no dispatch stub is needed.
    await orch.execute()

    # Reload to see final task statuses.
    from state.ledger import replay_ledger

    final_plan, _ = replay_ledger(tmp_path)
    assert final_plan is not None
    task_1_1 = next(t for t in final_plan.phases[0].tasks if t.id == "1.1")
    task_1_2 = next(t for t in final_plan.phases[0].tasks if t.id == "1.2")
    # Phase 3: only 1.2 is blocked; 1.1 stays pending (would have run
    # if our stub did anything).
    assert task_1_2.status == "blocked"
    assert "edit_scope_violation" in (task_1_2.blocked_reason or "")
    assert task_1_1.status == "pending"

    from state.ledger import read_entries

    ledger = read_entries(tmp_path)
    granular = [e for e in ledger if e.op == "task_blocked_scope_violation"]
    assert len(granular) == 1
    assert granular[0].payload["task_id"] == "1.2"


_PLAN_ALL_TASKS_VIOLATE = """
# Plan: Demo

EDIT_SCOPE:
  - src/math

## Phase 1: Implement
  - Acceptance:
    - [ ] cache lru implemented

### Task 1.1: violator a
  - Description: outside scope
  - Files: src/cache/lru.py
  - Acceptance:
    - [ ] cache lru implemented

### Task 1.2: violator b
  - Description: also outside scope
  - Files: src/dag/loader.py
  - Acceptance:
    - [ ] dag loader implemented
"""


@pytest.mark.asyncio
async def test_blanket_block_when_all_pending_tasks_violate(
    tmp_path: Path,
) -> None:
    """v0.26.2 back-compat: when every pending task violates the
    edit_scope, the blanket-block fallback still fires."""
    _init_repo(tmp_path)
    for d in ("src/cache", "src/dag"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "cache" / "lru.py").write_text(
        "# x\n", encoding="utf-8"
    )
    (tmp_path / "src" / "dag" / "loader.py").write_text(
        "# x\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
    subprocess.run(
        ["git", "commit", "-qm", "placeholders"],
        cwd=str(tmp_path),
        check=True,
    )

    adapter = StubAdapter(
        {
            "explorer": ok("found"),
            "domain_expert": ok("ok"),
            "architect": ok(_PLAN_ALL_TASKS_VIOLATE),
        }
    )
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    registry = build_registry(cfg)
    orch = Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-cascade-blanket",
    )

    plan = await orch.plan("Demo")
    assert plan is not None

    await orch.execute()

    from state.ledger import replay_ledger

    final_plan, _ = replay_ledger(tmp_path)
    assert final_plan is not None
    for task in final_plan.phases[0].tasks:
        assert task.status == "blocked"
        assert "edit_scope_violation" in (task.blocked_reason or "")
