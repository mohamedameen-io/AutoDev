"""Synthetic reproducer plans for the v0.27 hedge-repro integration test.

Three constants:

  * ``ARCHITECT_NOTES_IN_EDIT_SCOPE`` — the bare-token ``notes`` entry
    bug class. Mirrors :mod:`tests.test_orchestrator_plan_phase_drop`'s
    ``_BAD_PLAN_TWO_SCOPE_ENTRIES`` fixture (already in ``main`` at
    v0.26.2 and validated). Recoverable via the v0.26.2 persistent-drop.

  * ``ARCHITECT_PARENS_IN_TASK_FILES`` — a paren-hedged
    ``Task.files`` entry. v0.26.2 retry budget exhausts before recovery
    because the persistent-drop walks ``Plan.edit_scope`` only — the
    bug is at the task level. Phase 1 (Commit 3) is expected to
    upstream-reject this via the shape-check.

  * ``ARCHITECT_CLEAN_PLAN`` — the post-self-correction control. Used
    as the third item in the ``StubAdapter`` chain so the test can
    assert "v0.26.2 architect eventually fixes itself when it can".

All content here is synthetic: ``src/math/__init__.py`` is the only
real file the test repo needs (see
``tests/test_orchestrator_plan_phase_drop._init_git_repo_with_files``).
"""

from __future__ import annotations


ARCHITECT_NOTES_IN_EDIT_SCOPE = """\
# Plan: Add subtract

EDIT_SCOPE:
  - src/math
  - notes

## Phase 1: Implement

### Task 1.1: Add subtract function
  - Description: Implement subtract(a, b) and unit-test it.
  - Files: src/math/__init__.py
  - Acceptance:
    - [ ] subtract function exported
"""


ARCHITECT_PARENS_IN_TASK_FILES = """\
# Plan: Add subtract

EDIT_SCOPE:
  - src/math

## Phase 1: Implement

### Task 1.1: Add subtract function
  - Description: Implement subtract(a, b) and any follow-up helpers.
  - Files: src/math/__init__.py, (and any helper file identified during implementation)
  - Acceptance:
    - [ ] subtract function exported
"""


ARCHITECT_CLEAN_PLAN = """\
# Plan: Add subtract

EDIT_SCOPE:
  - src/math

## Phase 1: Implement

### Task 1.1: Add subtract function
  - Description: Implement subtract(a, b) and unit-test it.
  - Files: src/math/__init__.py
  - Acceptance:
    - [ ] subtract function exported
"""
