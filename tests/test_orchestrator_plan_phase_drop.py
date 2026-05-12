"""v0.26.2 Phase 3: bounded retry loop + persistent-failure drop tests.

The architect occasionally emits a bare-token `EDIT_SCOPE:` entry that
isn't a real path on disk (e.g. ``notes``). v0.26.1's single-shot retry
died with zero recoverable diagnostics when the architect repeated the
same malformation. v0.26.2 extends the retry to a 3-attempt loop with a
last-resort "drop the bad entry" rung — but ONLY when:

  1. the same ``(raw, reason)`` pair has recurred 3 times (so the
     architect had a fair chance to correct it via the typed retry
     envelope first), AND
  2. the drop does not leave ``plan.edit_scope == []`` (the documented
     whole-repo sentinel — silently widening scope is a P0 risk).

These six tests cover the happy drop path, the below-threshold case,
the empty-scope guard, the multi-entry surviving case, the
task.files coverage path, and the files_new opt-out coverage path.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator

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
        session_id="sess-test-drop",
    )


def _init_git_repo_with_files(cwd: Path, files: dict[str, str]) -> None:
    """Bootstrap a populated git repo with the given file map.

    ``files`` is a ``{repo_relative_path: contents}`` dict. The
    ``_RepoFileSnapshot`` validator only engages when ``git ls-files``
    returns at least one tracked entry, so the caller must commit at
    least one real file.
    """
    subprocess.run(["git", "init", "-q"], cwd=str(cwd), check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=str(cwd), check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=str(cwd), check=True
    )
    for rel, content in files.items():
        path = cwd / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(cwd), check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(cwd), check=True)


# Plan markdown: top-level EDIT_SCOPE with TWO entries (``src/math`` is
# real, ``notes`` is the bare-token malformation that fails validation).
# Task references the real ``src/math/__init__.py`` so we isolate the
# failure to the plan-level edit_scope.
_BAD_PLAN_TWO_SCOPE_ENTRIES = """
# Plan: Add subtract

EDIT_SCOPE:
  - src/math
  - notes

## Phase 1: Implement

### Task 1.1: real file
  - Description: edits an existing file
  - Files: src/math/__init__.py
  - Acceptance:
    - [ ] passes
"""


# Plan markdown: SINGLE EDIT_SCOPE entry that is the bad one. A drop
# would leave ``plan.edit_scope == []`` — the whole-repo sentinel — so
# the empty-scope guard must refuse.
_BAD_PLAN_SINGLE_SCOPE_ENTRY = """
# Plan: Add subtract

EDIT_SCOPE:
  - notes

## Phase 1: Implement

### Task 1.1: real file
  - Description: edits an existing file
  - Files: src/math/__init__.py
  - Acceptance:
    - [ ] passes
"""


# Plan markdown: task.files contains both a real file and the bad
# ``notes.md`` entry. No EDIT_SCOPE block at the plan level so the
# scope-drop site is in ``task.files`` only.
_BAD_PLAN_TASK_FILES = """
# Plan: Add subtract

## Phase 1: Implement

### Task 1.1: mixed files
  - Description: lists real + missing files
  - Files: src/math/__init__.py, notes.md
  - Acceptance:
    - [ ] passes
"""


# Plan markdown: task.files lists a path under the ``[new]`` opt-out.
# ``validate_files_exist`` skips ``files_new`` so no drop fires.
_GOOD_PLAN_FILES_NEW = """
# Plan: Add subtract

## Phase 1: Implement

### Task 1.1: creates a notes file
  - Description: creates a new notes file
  - Files: src/math/__init__.py, [new] notes.md
  - Acceptance:
    - [ ] passes
"""


# Clean plan with a single real file.
_GOOD_PLAN_CLEAN = """
# Plan: Add subtract

## Phase 1: Implement

### Task 1.1: real file
  - Description: edits an existing file
  - Files: src/math/__init__.py
  - Acceptance:
    - [ ] passes
"""


@pytest.mark.asyncio
async def test_drop_fires_only_after_third_recurrence(tmp_path: Path) -> None:
    """v0.26.2 Phase 3: architect emits the SAME bad plan 3 times. On the
    third validation, the persistent-failure drop fires: ``notes`` is
    dropped from ``plan.edit_scope``, a ``scope_entry_dropped`` ledger
    op is appended, and the run continues with the surviving scope.
    """
    _init_git_repo_with_files(
        tmp_path,
        {"src/math/__init__.py": "def add(a, b): return a + b\n"},
    )

    # Three identical bad plans — architect refuses to correct ``notes``.
    adapter = StubAdapter(
        {
            "explorer": ok("found stuff"),
            "domain_expert": ok("ok"),
            "architect": [
                ok(_BAD_PLAN_TWO_SCOPE_ENTRIES),
                ok(_BAD_PLAN_TWO_SCOPE_ENTRIES),
                ok(_BAD_PLAN_TWO_SCOPE_ENTRIES),
            ],
        }
    )
    orch = _make_orch(tmp_path, adapter)

    plan = await orch.plan("Add subtract")
    assert plan is not None
    assert "notes" not in plan.edit_scope
    assert "src/math" in plan.edit_scope
    # Three architect attempts in total (no fourth).
    assert adapter.count("architect") == 3

    # Ledger op was appended.
    from state.ledger import read_entries

    ledger = read_entries(tmp_path)
    drop_ops = [e for e in ledger if e.op == "scope_entry_dropped"]
    assert len(drop_ops) == 1, (
        f"expected one scope_entry_dropped, got {len(drop_ops)}"
    )
    payload = drop_ops[0].payload
    assert payload["path"] == "notes"
    assert payload["reason"] == "missing_on_disk"


@pytest.mark.asyncio
async def test_drop_below_threshold_re_raises_to_retry(tmp_path: Path) -> None:
    """v0.26.2 Phase 3: the architect emits the bad plan only twice then
    a clean plan on the third attempt. The drop must NOT fire — the
    architect corrected the malformation in time. No
    ``scope_entry_dropped`` op should appear in the ledger.
    """
    _init_git_repo_with_files(
        tmp_path,
        {"src/math/__init__.py": "def add(a, b): return a + b\n"},
    )
    adapter = StubAdapter(
        {
            "explorer": ok("found stuff"),
            "domain_expert": ok("ok"),
            "architect": [
                ok(_BAD_PLAN_TWO_SCOPE_ENTRIES),
                ok(_BAD_PLAN_TWO_SCOPE_ENTRIES),
                ok(_GOOD_PLAN_CLEAN),
            ],
        }
    )
    orch = _make_orch(tmp_path, adapter)

    plan = await orch.plan("Add subtract")
    assert plan is not None
    # Clean plan landed — no drop necessary.
    assert "notes" not in plan.edit_scope
    assert adapter.count("architect") == 3

    from state.ledger import read_entries

    ledger = read_entries(tmp_path)
    drop_ops = [e for e in ledger if e.op == "scope_entry_dropped"]
    assert drop_ops == [], (
        "no drop should fire when the architect self-corrects"
    )


@pytest.mark.asyncio
async def test_drop_refused_when_would_empty_plan_edit_scope(
    tmp_path: Path,
) -> None:
    """v0.26.2 Phase 3: when the bad entry is the ONLY ``plan.edit_scope``
    member, a drop would leave the list empty — and the empty list is
    the documented whole-repo sentinel. The guard MUST refuse the drop
    and re-raise the original :class:`PathValidationError`.
    """
    from orchestrator.path_validator import PathValidationError

    _init_git_repo_with_files(
        tmp_path,
        {"src/math/__init__.py": "def add(a, b): return a + b\n"},
    )
    adapter = StubAdapter(
        {
            "explorer": ok("found stuff"),
            "domain_expert": ok("ok"),
            "architect": [
                ok(_BAD_PLAN_SINGLE_SCOPE_ENTRY),
                ok(_BAD_PLAN_SINGLE_SCOPE_ENTRY),
                ok(_BAD_PLAN_SINGLE_SCOPE_ENTRY),
            ],
        }
    )
    orch = _make_orch(tmp_path, adapter)

    with pytest.raises(PathValidationError) as exc_info:
        await orch.plan("Add subtract")
    # Original error surfaces with the bad path.
    assert exc_info.value.raw == "notes"
    assert exc_info.value.reason == "missing_on_disk"

    # No drop ledger op fired — the guard prevented silent widening.
    from state.ledger import read_entries

    ledger = read_entries(tmp_path)
    drop_ops = [e for e in ledger if e.op == "scope_entry_dropped"]
    assert drop_ops == [], (
        "empty-scope guard must prevent the drop ledger op from firing"
    )


@pytest.mark.asyncio
async def test_drop_succeeds_when_other_scope_entries_remain(
    tmp_path: Path,
) -> None:
    """v0.26.2 Phase 3: when the plan has ``EDIT_SCOPE: src/math, notes``
    and the architect repeats it 3 times, the drop removes ``notes``
    and leaves ``plan.edit_scope == ["src/math"]``. Same shape as
    test #1 but explicit assertion on the surviving scope list.
    """
    _init_git_repo_with_files(
        tmp_path,
        {"src/math/__init__.py": "def add(a, b): return a + b\n"},
    )
    adapter = StubAdapter(
        {
            "explorer": ok("found stuff"),
            "domain_expert": ok("ok"),
            "architect": [
                ok(_BAD_PLAN_TWO_SCOPE_ENTRIES),
                ok(_BAD_PLAN_TWO_SCOPE_ENTRIES),
                ok(_BAD_PLAN_TWO_SCOPE_ENTRIES),
            ],
        }
    )
    orch = _make_orch(tmp_path, adapter)

    plan = await orch.plan("Add subtract")
    assert plan is not None
    # Only the surviving real entry remains.
    assert plan.edit_scope == ["src/math"]


@pytest.mark.asyncio
async def test_drop_removes_from_task_files_too(tmp_path: Path) -> None:
    """v0.26.2 Phase 3: drop logic must walk ``Task.files`` (not just
    ``Plan.edit_scope``). ``Files: src/math/__init__.py, notes.md`` →
    after the drop, ``task.files == ["src/math/__init__.py"]``.
    """
    _init_git_repo_with_files(
        tmp_path,
        {"src/math/__init__.py": "def add(a, b): return a + b\n"},
    )
    adapter = StubAdapter(
        {
            "explorer": ok("found stuff"),
            "domain_expert": ok("ok"),
            "architect": [
                ok(_BAD_PLAN_TASK_FILES),
                ok(_BAD_PLAN_TASK_FILES),
                ok(_BAD_PLAN_TASK_FILES),
            ],
        }
    )
    orch = _make_orch(tmp_path, adapter)

    plan = await orch.plan("Add subtract")
    assert plan is not None
    assert len(plan.phases) == 1
    task = plan.phases[0].tasks[0]
    assert task.files == ["src/math/__init__.py"], (
        f"expected only src/math/__init__.py to remain, got {task.files!r}"
    )
    assert "notes.md" not in task.files


@pytest.mark.asyncio
async def test_drop_does_not_touch_files_new(tmp_path: Path) -> None:
    """v0.26.2 Phase 3: ``[new]`` files are the architect's opt-out for
    paths the task itself will CREATE. The validator skips them per
    v0.24.3, so a plan with ``Files: real, [new] notes.md`` must
    validate cleanly on the FIRST attempt — no retry, no drop.
    """
    _init_git_repo_with_files(
        tmp_path,
        {"src/math/__init__.py": "def add(a, b): return a + b\n"},
    )
    adapter = StubAdapter(
        {
            "explorer": ok("found stuff"),
            "domain_expert": ok("ok"),
            "architect": ok(_GOOD_PLAN_FILES_NEW),
        }
    )
    orch = _make_orch(tmp_path, adapter)

    plan = await orch.plan("Add subtract")
    assert plan is not None
    # First attempt succeeded — no retry was needed.
    assert adapter.count("architect") == 1
    # The [new] entry is preserved on the task as files_new (not dropped).
    task = plan.phases[0].tasks[0]
    assert "notes.md" in task.files_new

    # No drop ledger op fired.
    from state.ledger import read_entries

    ledger = read_entries(tmp_path)
    drop_ops = [e for e in ledger if e.op == "scope_entry_dropped"]
    assert drop_ops == []
