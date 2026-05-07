"""Tests for the v0.6.1 ``Requires:`` / ``EXECUTABLE_BY:`` directives in
:mod:`src.orchestrator.plan_parser`.

The wider parser surface (plan title, phase/task headings, COMPLEXITY:) is
exercised in ``test_orchestrator_plan_phase.py``; these tests focus narrowly on
the new directives that mark a task as non-agent-executable.
"""

from __future__ import annotations

import pytest

from orchestrator.plan_parser import parse_plan_markdown


_PLAN_HEAD = "# Plan: Demo\n\n## Phase 1: Implement\n\n"


def _plan_with_task_body(body_lines: str) -> str:
    """Build a minimal plan markdown with one task whose body is ``body_lines``."""
    return (
        f"{_PLAN_HEAD}"
        f"### Task 1.1: do the thing\n"
        f"{body_lines}"
        f"  - Description: x\n"
    )


# ---------------------------------------------------------------------------
# Requires: parsing
# ---------------------------------------------------------------------------


def test_parse_requires_single_token() -> None:
    md = _plan_with_task_body("  - Requires: hardware\n")
    plan = parse_plan_markdown(md)
    task = plan.phases[0].tasks[0]
    assert task.requires == ["hardware"]


def test_parse_requires_multiple_tokens() -> None:
    """Comma-separated tokens are split, lowercased, and stripped."""
    md = _plan_with_task_body("  - Requires: hardware, human\n")
    plan = parse_plan_markdown(md)
    task = plan.phases[0].tasks[0]
    assert task.requires == ["hardware", "human"]


def test_parse_requires_uppercase_tokens_normalized() -> None:
    md = _plan_with_task_body("  - Requires: HARDWARE, External_Service\n")
    plan = parse_plan_markdown(md)
    task = plan.phases[0].tasks[0]
    assert task.requires == ["hardware", "external_service"]


def test_parse_requires_extra_whitespace_around_tokens() -> None:
    md = _plan_with_task_body("  - Requires:   hardware ,   human  \n")
    plan = parse_plan_markdown(md)
    task = plan.phases[0].tasks[0]
    assert task.requires == ["hardware", "human"]


def test_parse_unknown_requires_token_dropped(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Tokens outside the Literal set are dropped with a warning, not raised.

    autologging (structlog) renders to stdout; capsys captures the rendered
    line which includes the event name and the dropped token as kwargs.
    """
    md = _plan_with_task_body("  - Requires: hardware, mystery_token\n")
    plan = parse_plan_markdown(md)
    task = plan.phases[0].tasks[0]
    assert task.requires == ["hardware"]
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "plan_parser.unknown_requires_token" in combined
    assert "mystery_token" in combined


def test_parse_unknown_requires_token_only_yields_empty_list(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When all tokens are unknown, requires stays empty (not None) and a
    warning fires for each."""
    md = _plan_with_task_body("  - Requires: foo, bar\n")
    plan = parse_plan_markdown(md)
    task = plan.phases[0].tasks[0]
    assert task.requires == []
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    # Both unknown tokens should be reported.
    assert combined.count("plan_parser.unknown_requires_token") >= 2


# ---------------------------------------------------------------------------
# EXECUTABLE_BY: parsing (alternative form)
# ---------------------------------------------------------------------------


def test_parse_executable_by_human_maps_to_manual() -> None:
    md = _plan_with_task_body("  - EXECUTABLE_BY: human\n")
    plan = parse_plan_markdown(md)
    task = plan.phases[0].tasks[0]
    assert task.requires == ["manual"]


def test_parse_executable_by_human_case_insensitive() -> None:
    md = _plan_with_task_body("  - executable_by: HUMAN\n")
    plan = parse_plan_markdown(md)
    task = plan.phases[0].tasks[0]
    assert task.requires == ["manual"]


def test_parse_executable_by_agent_no_op() -> None:
    md = _plan_with_task_body("  - EXECUTABLE_BY: agent\n")
    plan = parse_plan_markdown(md)
    task = plan.phases[0].tasks[0]
    assert task.requires == []


# ---------------------------------------------------------------------------
# Backward-compat
# ---------------------------------------------------------------------------


def test_parse_legacy_plan_without_requires_field_returns_empty_list() -> None:
    """A v0.6.0-style task body (no ``Requires:`` line) still parses with
    ``task.requires == []`` thanks to ``default_factory=list``."""
    md = (
        f"{_PLAN_HEAD}"
        f"### Task 1.1: legacy task\n"
        f"  - Description: y\n"
    )
    plan = parse_plan_markdown(md)
    task = plan.phases[0].tasks[0]
    assert task.requires == []


def test_parse_requires_does_not_leak_across_tasks() -> None:
    """``Requires:`` on task 1.1 must not bleed into task 1.2."""
    md = (
        f"{_PLAN_HEAD}"
        f"### Task 1.1: needs hardware\n"
        f"  - Requires: hardware\n"
        f"  - Description: x\n\n"
        f"### Task 1.2: pure software\n"
        f"  - Description: y\n"
    )
    plan = parse_plan_markdown(md)
    t11, t12 = plan.phases[0].tasks
    assert t11.requires == ["hardware"]
    assert t12.requires == []


def test_parse_requires_combined_with_other_directives() -> None:
    """``Requires:`` coexists with ``Files:``, ``Description:``, ``Acceptance:``,
    ``Depends:`` — the parser must keep them all."""
    md = (
        f"{_PLAN_HEAD}"
        f"### Task 1.1: complex task\n"
        f"  - Description: do it\n"
        f"  - Files: a.py, b.py\n"
        f"  - Requires: hardware\n"
        f"  - Depends: 1.0\n"
        f"  - Acceptance:\n"
        f"    - [ ] passes\n"
    )
    plan = parse_plan_markdown(md)
    task = plan.phases[0].tasks[0]
    assert task.requires == ["hardware"]
    assert task.files == ["a.py", "b.py"]
    assert task.depends_on == ["1.0"]
    assert len(task.acceptance) == 1
    assert task.description == "do it"
