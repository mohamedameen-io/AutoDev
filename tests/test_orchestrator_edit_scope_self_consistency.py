"""Field-finding F-1: a plan's own task-declared files are in-scope.

Reproduces the P1/task_002 field failure: a phase declared
``edit_scope = ["index.js"]`` while the SAME plan tasked an edit to
``test_index.js`` (the test file). At execute pre-flight the per-task
edit_scope check (:func:`orchestrator.dag.collect_edit_scope_violations`)
raised ``edit_scope_violation`` for ``test_index.js`` and the task ended
at ``task_blocked_scope_violation`` with an empty diff (no delivery).

The plan was internally inconsistent: it tasked an edit to a file not in
its own phase ``edit_scope``. A task is, by definition, permitted to edit
the files IT (or another task in the plan) declares — those files are part
of the plan's intent. The fix makes the plan self-consistent at INIT: each
phase's effective ``edit_scope`` is extended to include the concrete files
its tasks declare (``files`` ∪ ``files_new``). See
:func:`orchestrator.dependency_inference.repair_phase_edit_scope`.

Placement note: the repair runs at plan INIT, AFTER the on-disk drop /
empty-scope guard pass (in
:func:`orchestrator.plan_phase._validate_with_persistent_drop`), NOT at
parse time — running it before the drop would pre-admit task files and mask
the P0 empty-scope guard. ``parse_plan_markdown`` therefore does NOT repair;
the tests below that parse a plan apply ``repair_phase_edit_scope``
explicitly to exercise the same post-parse step plan-init performs.

Safety boundary preserved: the file the *developer actually edits* is
still validated at apply-time against the resolved ``edit_scope``
(:meth:`orchestrator.worktree.WorktreeManager.apply_patch_to_main`). A
diff hunk targeting a file declared by NO task and outside the scope is
still rejected — the negative tests below pin that the enforcement was
NOT disabled.
"""

from __future__ import annotations

import pytest

from orchestrator.dag import (
    EditScopeViolation,
    collect_edit_scope_violations,
    is_in_scope,
)
from orchestrator.dependency_inference import repair_phase_edit_scope
from orchestrator.plan_parser import parse_plan_markdown
from state.schemas import Phase, Task


# ---------------------------------------------------------------------------
# Positive: a plan's own task-declared file is in-scope (the F-1 fix)
# ---------------------------------------------------------------------------


_P1_REPRO_MD = """# Plan: implement feature

## Phase 1: Implement
EDIT_SCOPE:
  - index.js
### Task 1.1: Implement feature in index.js
- Description: add the feature to index.js
- Files: index.js
### Task 1.2: Add tests for the feature
- Description: cover the new feature with tests
- Files: test_index.js
"""


def test_parsed_plan_task_declared_file_is_in_scope() -> None:
    """P1 repro: a task declaring ``test_index.js`` is NOT a scope violation
    even though the phase ``edit_scope`` only lists ``index.js`` — the task's
    own declared file is part of the plan's intent."""
    plan = parse_plan_markdown(_P1_REPRO_MD)
    # Plan-init applies the repair AFTER parse (see module docstring).
    repair_phase_edit_scope(plan.phases, plan_edit_scope=plan.edit_scope)
    # The phase scope is widened to admit the files its own tasks declare.
    assert plan.phases[0].edit_scope is not None
    assert "index.js" in plan.phases[0].edit_scope
    assert "test_index.js" in plan.phases[0].edit_scope
    # The pre-flight per-task check now finds NO violation for the plan's
    # own tasks (RED before the fix: it flagged test_index.js).
    violations = collect_edit_scope_violations(plan)
    assert violations == [], [str(v) for v in violations]


def test_cross_task_declared_file_is_in_scope() -> None:
    """A task may touch a file ANOTHER task in the same phase declared.

    Task 1.2 reads/edits ``index.js`` (declared by 1.1) AND its own
    ``test_index.js``; neither is a violation."""
    md = """# Plan: cross-task

## Phase 1: Implement
EDIT_SCOPE:
  - src/core
### Task 1.1: Create the serializer
- Description: build src/core/serializer.py
- Files: src/core/serializer.py
### Task 1.2: Route through the serializer and test it
- Description: wire routing + add the test
- Files: src/core/serializer.py, tests/test_serializer.py
"""
    plan = parse_plan_markdown(md)
    repair_phase_edit_scope(plan.phases, plan_edit_scope=plan.edit_scope)
    assert "tests/test_serializer.py" in (plan.phases[0].edit_scope or [])
    assert collect_edit_scope_violations(plan) == []


def test_repair_extends_inherited_plan_scope_when_phase_scope_none() -> None:
    """When a phase inherits the plan scope (``Phase.edit_scope is None``)
    and a task declares an out-of-scope file, the repair materializes the
    phase scope = plan scope ∪ task files (so the inherit case is fixed too)."""
    md = """# Plan: inherit

EDIT_SCOPE:
  - index.js

## Phase 1: Implement
### Task 1.1: Implement + test
- Description: implement and test
- Files: index.js, test_index.js
"""
    plan = parse_plan_markdown(md)
    assert plan.edit_scope == ["index.js"]
    # Parse alone leaves the phase override unset (inherit).
    assert plan.phases[0].edit_scope is None
    repair_phase_edit_scope(plan.phases, plan_edit_scope=plan.edit_scope)
    # Phase had no per-phase override; the repair materializes one ONLY
    # because there is an out-of-scope task file to admit.
    assert plan.phases[0].edit_scope is not None
    assert "index.js" in plan.phases[0].edit_scope
    assert "test_index.js" in plan.phases[0].edit_scope
    assert collect_edit_scope_violations(plan) == []


# ---------------------------------------------------------------------------
# Negative / safety: enforcement is NOT disabled
# ---------------------------------------------------------------------------


def test_repair_is_noop_when_no_narrowing_scope() -> None:
    """When the resolved scope is empty (whole-repo, no constraint), the
    repair must NOT materialize a phase scope — that would turn a
    whole-repo phase into a narrowed one. ``Phase.edit_scope`` stays None."""
    md = """# Plan: legacy whole-repo

## Phase 1: Implement
### Task 1.1: Implement + test
- Description: implement and test anywhere
- Files: index.js, test_index.js
"""
    plan = parse_plan_markdown(md)
    assert plan.edit_scope == []
    repair_phase_edit_scope(plan.phases, plan_edit_scope=plan.edit_scope)
    # No narrowing scope (empty == whole-repo) → repair is a no-op → inherit
    # (None) preserved; materializing a scope here would silently narrow.
    assert plan.phases[0].edit_scope is None
    assert collect_edit_scope_violations(plan) == []


def test_repair_does_not_admit_a_file_no_task_declares() -> None:
    """The repair only admits files SOME task declares. A phase scope that
    narrows to ``index.js`` must NOT silently grow to cover an unrelated
    path no task declares — the boundary against undeclared files holds.

    Constructed directly (bypassing the parser) so we can assert the
    repair leaves an undeclared path out of the resolved scope.
    """
    phase = Phase(
        id="1",
        title="Implement",
        tasks=[
            Task(
                id="1.1",
                phase_id="1",
                title="impl",
                description="d",
                files=["index.js", "test_index.js"],
            )
        ],
        edit_scope=["index.js"],
    )
    repair_phase_edit_scope([phase], plan_edit_scope=["index.js"])
    resolved = phase.edit_scope or []
    # The task's own files are admitted...
    assert is_in_scope("index.js", resolved)
    assert is_in_scope("test_index.js", resolved)
    # ...but a file NO task declared is still out of scope.
    assert not is_in_scope("secrets/leak.txt", resolved)


def test_apply_time_boundary_still_rejects_undeclared_out_of_scope_file() -> None:
    """Safety boundary preserved at APPLY time: a diff hunk that targets a
    file declared by NO task and outside ``edit_scope`` is still rejected by
    :meth:`WorktreeManager.apply_patch_to_main`'s pre-flight scope check.

    This is the genuine "developer edited an undeclared file" guard. The
    F-1 fix touches only plan-init scope resolution; it does NOT relax this
    check, so enforcement on the developer's ACTUAL edits survives.
    """
    import asyncio
    from pathlib import Path

    from orchestrator.worktree import WorktreeManager

    # A diff that edits a file outside the resolved edit_scope and which no
    # task declared. The pre-flight scope check fires before any git apply.
    out_of_scope_diff = (
        "diff --git a/secrets/leak.txt b/secrets/leak.txt\n"
        "--- a/secrets/leak.txt\n"
        "+++ b/secrets/leak.txt\n"
        "@@ -0,0 +1 @@\n"
        "+exfiltrate\n"
    )

    class _StubMgr(WorktreeManager):
        def __init__(self) -> None:
            # Bypass the heavy __init__ — we only exercise the pre-flight
            # scope guard, which runs before any filesystem/git access.
            pass

        async def get_diff_vs_base(  # type: ignore[override]
            self, worktree: Path, base_ref: str = "HEAD"
        ) -> str:
            return out_of_scope_diff

    mgr = _StubMgr()
    with pytest.raises(EditScopeViolation) as exc:
        asyncio.run(
            mgr.apply_patch_to_main(
                Path("/tmp/does-not-matter"),
                edit_scope=["index.js"],
            )
        )
    assert "secrets/leak.txt" in str(exc.value)
