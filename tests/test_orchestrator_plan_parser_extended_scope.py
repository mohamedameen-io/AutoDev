"""v0.20.0 C1: plan_parser parses Extended-scope: blocks."""

from __future__ import annotations

from orchestrator.plan_parser import parse_plan_markdown


_PLAN_WITH_EXTENDED = """\
# Plan: ext-test

## Phase 1: Implementation

### Task 1.1: refactor module
- Description: refactor X
- Files: src/orchestrator/x.py
- Extended-scope: src/qa, tests/qa/

### Task 1.2: regular task
- Description: regular
- Files: src/orchestrator/y.py
"""


_PLAN_NO_EXTENDED = """\
# Plan: legacy

## Phase 1: Stuff

### Task 1.1: regular
- Description: r
- Files: src/orchestrator/x.py
"""


def test_parse_plan_extracts_extended_scope_inline() -> None:
    plan = parse_plan_markdown(_PLAN_WITH_EXTENDED)
    task = plan.phases[0].tasks[0]
    assert task.extended_scope == ["src/qa", "tests/qa"]


def test_parse_plan_default_extended_scope_empty() -> None:
    plan = parse_plan_markdown(_PLAN_WITH_EXTENDED)
    task2 = plan.phases[0].tasks[1]
    assert task2.extended_scope == []


def test_parse_plan_no_extended_scope_block_yields_empty_list() -> None:
    plan = parse_plan_markdown(_PLAN_NO_EXTENDED)
    assert plan.phases[0].tasks[0].extended_scope == []


def test_parse_plan_extended_scope_strips_trailing_slashes() -> None:
    """``- Extended-scope: src/qa/, tests/qa/`` normalizes to no trailing /."""
    md = """\
# Plan: trailer

## Phase 1: P1

### Task 1.1: refactor
- Description: r
- Extended-scope: src/qa/, tests/qa/
"""
    plan = parse_plan_markdown(md)
    assert plan.phases[0].tasks[0].extended_scope == ["src/qa", "tests/qa"]


def test_parse_plan_underscore_form_also_recognized() -> None:
    """``Extended_scope:`` (underscore) is treated identically to dash form."""
    md = """\
# Plan: underscore

## Phase 1: P

### Task 1.1: t
- Description: d
- Extended_scope: src/foo
"""
    plan = parse_plan_markdown(md)
    assert plan.phases[0].tasks[0].extended_scope == ["src/foo"]
