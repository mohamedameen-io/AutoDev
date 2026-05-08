"""v0.14.0 plan-parser tests for the ``EDIT_SCOPE:`` block.

The architect emits an optional top-level block::

    EDIT_SCOPE:
      - src/foo
      - tests/foo

after the ``# Plan:`` heading and before the first ``## Phase`` heading.
Per-phase overrides use the same ``EDIT_SCOPE:`` block placed inside a
phase body before the first ``### Task`` heading. Empty / missing
blocks parse to an empty list (no constraint).
"""

from __future__ import annotations

from orchestrator.plan_parser import parse_plan_markdown


def test_parse_edit_scope_top_level_block() -> None:
    """A top-level ``EDIT_SCOPE:`` block lands on ``Plan.edit_scope``."""
    md = """# Plan: scoped

EDIT_SCOPE:
  - src/foo
  - tests/foo

## Phase 1: Setup
### Task 1.1: Add foo
- Description: implements foo
- Files: src/foo/__init__.py
"""
    plan = parse_plan_markdown(md)
    assert plan.edit_scope == ["src/foo", "tests/foo"]
    # Per-phase override absent → None (inherit semantics).
    assert plan.phases[0].edit_scope is None


def test_parse_edit_scope_per_phase_override() -> None:
    """A per-phase ``EDIT_SCOPE:`` block lands on ``Phase.edit_scope``,
    overriding the plan-level scope for that phase only."""
    md = """# Plan: scoped

EDIT_SCOPE:
  - src

## Phase 1: Narrow phase
EDIT_SCOPE:
  - src/foo
### Task 1.1: Tight task
- Description: only foo
- Files: src/foo/x.py

## Phase 2: Wide phase
### Task 2.1: Wide task
- Description: anywhere
- Files: src/anywhere.py
"""
    plan = parse_plan_markdown(md)
    assert plan.edit_scope == ["src"]
    assert plan.phases[0].edit_scope == ["src/foo"]
    # Phase 2 does NOT override → inherits via None.
    assert plan.phases[1].edit_scope is None


def test_parse_edit_scope_missing_block_yields_empty_list() -> None:
    """Plans without the block parse cleanly with empty edit_scope —
    legacy migration guarantee."""
    md = """# Plan: legacy

## Phase 1: Setup
### Task 1.1: Do thing
- Description: doing
- Files: a.py
"""
    plan = parse_plan_markdown(md)
    assert plan.edit_scope == []
    assert plan.phases[0].edit_scope is None


def test_parse_edit_scope_strips_comments() -> None:
    """Trailing ``#`` comments on a scope entry are stripped, leading
    whitespace is tolerated."""
    md = """# Plan: scoped

EDIT_SCOPE:
  - src/foo  # main package
  - tests    # tests subtree

## Phase 1: Setup
### Task 1.1: foo
- Description: doing
- Files: src/foo/x.py
"""
    plan = parse_plan_markdown(md)
    assert plan.edit_scope == ["src/foo", "tests"]


def test_parse_edit_scope_strips_trailing_slash() -> None:
    """Trailing ``/`` on entries is stripped (handled by the schema
    validator). The parser doesn't need its own normalization but
    must not block the schema from doing so."""
    md = """# Plan: scoped

EDIT_SCOPE:
  - src/
  - tests/

## Phase 1: Setup
### Task 1.1: foo
- Description: doing
- Files: src/foo.py
"""
    plan = parse_plan_markdown(md)
    assert plan.edit_scope == ["src", "tests"]
