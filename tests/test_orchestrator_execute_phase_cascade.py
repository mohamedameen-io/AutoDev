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


# ---------------------------------------------------------------------------
# F-1 (edit_scope self-consistency) reshaped these execute-phase cascade
# tests. The repair (``orchestrator.dependency_inference.repair_phase_edit_scope``,
# applied at plan init AFTER the on-disk drop pass) now admits the CONCRETE
# files a task declares into its phase ``edit_scope`` — so a concrete
# out-of-scope ``Task.files`` entry is no longer a pre-flight violation (it is
# the plan's own intent). The cascade machinery
# (``collect_edit_scope_violations`` → block-only-violators vs. blanket-block,
# in ``execute_phase``) is therefore exercised against the RESIDUAL case the
# repair deliberately does NOT cover: a GLOB declaration.
#
# Why the plan is seeded via ``init_plan`` rather than ``orch.plan``: a glob in
# ``Task.files`` (e.g. ``src/cache/**``) is rejected upstream by the
# file-existence validator (``validate_files_exist`` checks the entry as a
# literal path → ``missing_on_disk``), so the plan never reaches execute when
# built through the architect path. The file-existence check is a separate,
# earlier concern from the edit_scope cascade under test here, so we seed the
# already-validated plan straight into the ledger — exactly the hand-off
# ``run_plan_phase`` performs in production via ``init_plan``. The repair runs
# inside ``run_plan_phase``/parse, NOT in ``init_plan``; seeding a hand-built
# plan keeps the glob intact so the cascade fires.


def _build_cascade_orch(tmp_path: Path, session_id: str) -> Orchestrator:
    """Init a git repo with ``src/math`` + out-of-scope ``src/cache`` and
    ``src/dag`` dirs (committed so ``git ls-files`` → ``orch.tracked_files``
    sees them and the glob expands), and return a tournaments-off orchestrator.
    """
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
        ["git", "commit", "-qm", "placeholders"], cwd=str(tmp_path), check=True
    )
    adapter = StubAdapter(
        {"explorer": ok("found"), "domain_expert": ok("ok")}
    )
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    registry = build_registry(cfg)
    return Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id=session_id,
    )


def _glob_task(task_id: str, glob: str) -> Task:
    return Task(
        id=task_id,
        phase_id="1",
        title=f"task {task_id}",
        description=f"task {task_id} declares glob {glob}",
        files=[glob],
        acceptance=[AcceptanceCriterion(id=f"ac-{task_id}", description="ok")],
        assigned_agent="developer",
    )


def _seed_plan(tasks: list[Task], *, session_id: str) -> Plan:
    return Plan(
        plan_id=f"p-{session_id}",
        spec_hash="abc",
        phases=[Phase(id="1", title="impl", tasks=tasks)],
        metadata={"title": "Demo"},
        edit_scope=["src/math"],
        created_at="2026-05-12T00:00:00Z",
        updated_at="2026-05-12T00:00:00Z",
    )


@pytest.mark.asyncio
async def test_only_violating_task_blocked_siblings_remain_pending(
    tmp_path: Path,
) -> None:
    """v0.27 behaviour (residual glob case): only the offending task is
    blocked; in-scope siblings survive as pending.

    Task 1.1 declares a concrete in-scope file (``src/math/__init__.py``);
    Task 1.2 declares an out-of-scope GLOB (``src/cache/**``) the repair does
    not admit. The execute pre-flight expands the glob against tracked files,
    flags only 1.2, and the task-scoped block fires for 1.2 alone."""
    orch = _build_cascade_orch(tmp_path, "sess-cascade")
    in_scope = Task(
        id="1.1",
        phase_id="1",
        title="real file",
        description="this stays in scope",
        files=["src/math/__init__.py"],
        acceptance=[AcceptanceCriterion(id="ac-1.1", description="ok")],
        assigned_agent="developer",
    )
    plan = _seed_plan(
        [in_scope, _glob_task("1.2", "src/cache/**")],
        session_id="sess-cascade",
    )
    await orch.plan_manager.init_plan(plan)

    # The cascade-block fires BEFORE any task runs and returns the
    # empty processed list, so no developer dispatch stub is needed.
    await orch.execute()

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


@pytest.mark.asyncio
async def test_blanket_block_when_all_pending_tasks_violate(
    tmp_path: Path,
) -> None:
    """v0.26.2 back-compat (residual glob case): when EVERY pending task
    violates the edit_scope, the blanket-block fallback still fires.

    Both tasks declare out-of-scope GLOBs the repair does not admit, so the
    pre-flight flags both; with no in-scope pending task left, the blanket
    block fires."""
    orch = _build_cascade_orch(tmp_path, "sess-cascade-blanket")
    plan = _seed_plan(
        [_glob_task("1.1", "src/cache/**"), _glob_task("1.2", "src/dag/**")],
        session_id="sess-cascade-blanket",
    )
    await orch.plan_manager.init_plan(plan)

    await orch.execute()

    from state.ledger import replay_ledger

    final_plan, _ = replay_ledger(tmp_path)
    assert final_plan is not None
    for task in final_plan.phases[0].tasks:
        assert task.status == "blocked"
        assert "edit_scope_violation" in (task.blocked_reason or "")


_PLAN_CONCRETE_OUT_OF_SCOPE_FILE = """
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

### Task 1.2: declares a concrete file outside the narrowed scope
  - Description: the plan ITSELF tasks editing this file
  - Files: src/cache/lru.py
  - Acceptance:
    - [ ] cache lru implemented
"""


@pytest.mark.asyncio
async def test_concrete_task_declared_file_admitted_through_plan_pipeline(
    tmp_path: Path,
) -> None:
    """F-1 admit behavior, end-to-end through the real ``orch.plan`` pipeline
    (parse → on-disk validate → repair → init_plan).

    A task declaring a CONCRETE file outside the phase's narrowed
    ``edit_scope`` is no longer a violation: the plan-init repair admits the
    file (the plan's own intent), so ``collect_edit_scope_violations`` is empty
    and the task is NOT blocked at execute pre-flight. This is the inverse of
    the two cascade tests above — together they pin that the repair admits
    concrete declarations while the collector still guards the residual glob
    case."""
    _init_repo(tmp_path)
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
            "architect": ok(_PLAN_CONCRETE_OUT_OF_SCOPE_FILE),
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
        session_id="sess-cascade-admit",
    )

    plan = await orch.plan("Demo")
    assert plan is not None
    # The repair widened the phase scope to admit the task's own declared
    # concrete file — no pre-flight violation remains.
    assert "src/cache/lru.py" in (plan.phases[0].edit_scope or [])
    assert collect_edit_scope_violations(
        plan, tracked_files=orch.tracked_files
    ) == []
