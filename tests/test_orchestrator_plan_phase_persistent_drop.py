"""v0.27 Phase 4 (audit §4): generalised persistent-drop tests.

Five scenarios beyond the v0.26.2 baseline:

  1. ``task_files_entry_dropped`` emitted alongside the catch-all
     ``scope_entry_dropped`` when the drop touches a task.files entry.
  2. ``phase_edit_scope_entry_dropped`` emitted when the drop touches
     a phase-level override list.
  3. Phase-edit-scope empty-guard refuses to silently widen a non-None
     phase scope back to the plan-level scope.
  4. ``task_auto_skipped`` fires when all of a task's files + files_new
     are dropped, transitioning the task to ``status="skipped"``.
  5. ``architect_persistent_parse_error`` fires after three consecutive
     :class:`PlanParseError` recurrences (and the same shape applies
     for :class:`PydValidationError`).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator.path_validator import PathValidationError

from stub_adapter import StubAdapter, ok


def _make_orch(cwd: Path, adapter: StubAdapter) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    registry = build_registry(cfg)
    return Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-test-persistent-drop",
    )


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


_PLAN_WITH_BAD_TASK_FILES = """
# Plan: Demo

EDIT_SCOPE:
  - src/math

## Phase 1: Implement
  - Acceptance:
    - [ ] subtract function exported

### Task 1.1: real-and-bogus
  - Description: real file plus a token that fails validation
  - Files: src/math/__init__.py, src/math/missing.py
  - Acceptance:
    - [ ] subtract function exported
"""


@pytest.mark.asyncio
async def test_task_files_entry_dropped_op_fires(tmp_path: Path) -> None:
    """Drop on ``task.files`` emits the granular op alongside
    ``scope_entry_dropped`` so forensics can pinpoint the task."""
    _init_repo(tmp_path)
    adapter = StubAdapter(
        {
            "explorer": ok("found"),
            "domain_expert": ok("ok"),
            "architect": [
                ok(_PLAN_WITH_BAD_TASK_FILES),
                ok(_PLAN_WITH_BAD_TASK_FILES),
                ok(_PLAN_WITH_BAD_TASK_FILES),
            ],
        }
    )
    orch = _make_orch(tmp_path, adapter)
    plan = await orch.plan("Add subtract")
    assert plan is not None

    from state.ledger import read_entries

    ledger = read_entries(tmp_path)
    ops_by_name = {e.op: e for e in ledger if e.op.endswith("_dropped")}
    assert "scope_entry_dropped" in ops_by_name
    assert "task_files_entry_dropped" in ops_by_name
    payload = ops_by_name["task_files_entry_dropped"].payload
    assert payload["path"] == "src/math/missing.py"
    assert payload["task_id"] == "1.1"


_PLAN_WITH_BAD_PHASE_SCOPE_PLUS_TASK = """
# Plan: Demo

EDIT_SCOPE:
  - src/math

## Phase 1: Implement
EDIT_SCOPE:
  - src/math
  - src/missing

  - Acceptance:
    - [ ] subtract function exported

### Task 1.1: real file
  - Description: real file
  - Files: src/math/__init__.py
  - Acceptance:
    - [ ] subtract function exported
"""


@pytest.mark.asyncio
async def test_phase_edit_scope_entry_dropped_op_fires(
    tmp_path: Path,
) -> None:
    """Drop on a phase-level edit_scope override emits the granular op
    pinned with phase_id."""
    _init_repo(tmp_path)
    adapter = StubAdapter(
        {
            "explorer": ok("found"),
            "domain_expert": ok("ok"),
            "architect": [
                ok(_PLAN_WITH_BAD_PHASE_SCOPE_PLUS_TASK),
                ok(_PLAN_WITH_BAD_PHASE_SCOPE_PLUS_TASK),
                ok(_PLAN_WITH_BAD_PHASE_SCOPE_PLUS_TASK),
            ],
        }
    )
    orch = _make_orch(tmp_path, adapter)
    plan = await orch.plan("Add subtract")
    assert plan is not None

    from state.ledger import read_entries

    ledger = read_entries(tmp_path)
    granular = [
        e for e in ledger if e.op == "phase_edit_scope_entry_dropped"
    ]
    assert len(granular) >= 1
    assert granular[0].payload["phase_id"] == "1"
    assert granular[0].payload["path"] == "src/missing"


_PLAN_WHERE_PHASE_SCOPE_GOES_EMPTY = """
# Plan: Demo

EDIT_SCOPE:
  - src/math

## Phase 1: Implement
EDIT_SCOPE:
  - src/missing

  - Acceptance:
    - [ ] subtract function exported

### Task 1.1: real file
  - Description: real file
  - Files: src/math/__init__.py
  - Acceptance:
    - [ ] subtract function exported
"""


@pytest.mark.asyncio
async def test_phase_edit_scope_empty_guard_refuses_drop(
    tmp_path: Path,
) -> None:
    """When dropping would empty a non-None phase override, the guard
    refuses (silent widening to plan scope would be a P0 risk)."""
    _init_repo(tmp_path)
    adapter = StubAdapter(
        {
            "explorer": ok("found"),
            "domain_expert": ok("ok"),
            "architect": [
                ok(_PLAN_WHERE_PHASE_SCOPE_GOES_EMPTY),
                ok(_PLAN_WHERE_PHASE_SCOPE_GOES_EMPTY),
                ok(_PLAN_WHERE_PHASE_SCOPE_GOES_EMPTY),
            ],
        }
    )
    orch = _make_orch(tmp_path, adapter)

    with pytest.raises(PathValidationError):
        await orch.plan("Add subtract")


_PLAN_WHERE_TASK_FILES_GO_EMPTY = """
# Plan: Demo

EDIT_SCOPE:
  - src/math

## Phase 1: Implement
  - Acceptance:
    - [ ] documentation written

### Task 1.1: real file
  - Description: real file
  - Files: src/math/__init__.py
  - Acceptance:
    - [ ] subtract function exported

### Task 1.2: dead task
  - Description: this task references only a bogus path
  - Files: src/math/missing.py
  - Acceptance:
    - [ ] never gates
"""


@pytest.mark.asyncio
async def test_task_auto_skipped_when_all_files_dropped(
    tmp_path: Path,
) -> None:
    """A task whose only Files entry is dropped (leaving both
    ``files`` and ``files_new`` empty) is auto-transitioned to
    ``skipped`` and a ``task_auto_skipped`` op records the action."""
    _init_repo(tmp_path)
    adapter = StubAdapter(
        {
            "explorer": ok("found"),
            "domain_expert": ok("ok"),
            "architect": [
                ok(_PLAN_WHERE_TASK_FILES_GO_EMPTY),
                ok(_PLAN_WHERE_TASK_FILES_GO_EMPTY),
                ok(_PLAN_WHERE_TASK_FILES_GO_EMPTY),
            ],
        }
    )
    orch = _make_orch(tmp_path, adapter)
    plan = await orch.plan("Add subtract")
    assert plan is not None

    task_1_2 = plan.phases[0].tasks[1]
    assert task_1_2.id == "1.2"
    assert task_1_2.status == "skipped"
    assert task_1_2.files == []
    assert task_1_2.files_new == []

    from state.ledger import read_entries

    ledger = read_entries(tmp_path)
    skipped_ops = [e for e in ledger if e.op == "task_auto_skipped"]
    assert len(skipped_ops) == 1
    assert skipped_ops[0].payload["task_id"] == "1.2"


@pytest.mark.asyncio
async def test_architect_persistent_parse_error_op_fires(
    tmp_path: Path,
) -> None:
    """Three consecutive :class:`PlanParseError` recurrences trigger
    the ``architect_persistent_parse_error`` op (audit-only telemetry).
    The run still surfaces the parse error at the end of the retry
    budget."""
    from orchestrator.plan_parser import PlanParseError as _PPE

    _init_repo(tmp_path)
    bad_md = "this is not a plan"
    adapter = StubAdapter(
        {
            "explorer": ok("found"),
            "domain_expert": ok("ok"),
            "architect": [ok(bad_md), ok(bad_md), ok(bad_md)],
        }
    )
    orch = _make_orch(tmp_path, adapter)

    with pytest.raises(_PPE):
        await orch.plan("Add subtract")

    from state.ledger import read_entries

    ledger = read_entries(tmp_path)
    persistent_ops = [
        e for e in ledger if e.op == "architect_persistent_parse_error"
    ]
    assert len(persistent_ops) >= 1
    payload = persistent_ops[0].payload
    assert payload["exc_class"] == "PlanParseError"
    assert payload["recurrence_count"] >= 3
