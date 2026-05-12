"""v0.27 Phase 1: unit tests for ``_normalize_path_entry`` + ``ParsedFilesReport``.

The helper is the single source of truth for the shape-check applied
to architect-emitted path entries across the three parser sites:
``Files:``, ``EDIT_SCOPE:``, and ``Extended-scope:``. Phase 0
exercises the helper end-to-end through the
:mod:`tests.fixtures.malformed_architect_outputs` parametrised test;
this module covers the helper directly so a parser regression
surfaces in milliseconds rather than as a downstream validation
failure.
"""

from __future__ import annotations

import pytest

from orchestrator.plan_parser import (
    ParsedFilesReport,
    _normalize_path_entry,
)


# ---------------------------------------------------------------------------
# Drop cases — should return path=None with a structured drop_reason.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected_reason",
    [
        ("", "empty"),
        ("   ", "empty"),
        ("# just a comment", "comment_only"),
        ("(and any helper file)", "paren_hedge"),
        ("src/foo.py (legacy)", "paren_hedge"),
        ("src/foo.py)", "paren_hedge"),
        ("[unmatched", "bracket_hedge"),
        ("trailing]", "bracket_hedge"),
        ("TBD", "placeholder"),
        ("todo", "placeholder"),
        ("N/A", "placeholder"),
        ("none", "placeholder"),
        ("FIXME", "placeholder"),
        ("my notes", "space_without_slash"),
        ("two words no slash", "space_without_slash"),
        ("/", "empty_after_strip"),
    ],
)
def test_normalize_path_entry_drops(raw: str, expected_reason: str) -> None:
    report = _normalize_path_entry(raw)
    assert report.path is None, (
        f"expected drop for {raw!r}, got path={report.path!r}"
    )
    assert report.drop_reason == expected_reason, (
        f"{raw!r}: expected reason={expected_reason!r}, "
        f"got {report.drop_reason!r}"
    )


# ---------------------------------------------------------------------------
# Keep cases — return path=normalized with drop_reason=None.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected_path",
    [
        ("src/math/__init__.py", "src/math/__init__.py"),
        ("  src/math  ", "src/math"),
        ("src/math/", "src/math"),  # trailing / stripped
        ("docs/My File.md", "docs/My File.md"),  # space WITH slash → kept
        ("notes", "notes"),  # bare token (no space, no slash) → kept
        ("README", "README"),
        ("src/foo.py", "src/foo.py"),
        ("src/foo.py     ", "src/foo.py"),  # whitespace tail stripped
        # Inline-comment tail is stripped but the head survives.
        ("src/foo.py # main entry", "src/foo.py"),
    ],
)
def test_normalize_path_entry_keeps(raw: str, expected_path: str) -> None:
    report = _normalize_path_entry(raw)
    assert report.path == expected_path
    assert report.drop_reason is None
    assert report.raw == raw


# ---------------------------------------------------------------------------
# Report shape contract.
# ---------------------------------------------------------------------------


def test_parsed_files_report_is_frozen_dataclass() -> None:
    """``ParsedFilesReport`` is immutable so callers can safely cache."""
    report = _normalize_path_entry("src/foo.py")
    assert isinstance(report, ParsedFilesReport)
    with pytest.raises(Exception):
        report.path = "other"  # type: ignore[misc]


def test_dropped_entry_preserves_raw() -> None:
    """Caller's logging path relies on ``raw`` being the verbatim input."""
    raw = "src/foo.py (deprecated)"
    report = _normalize_path_entry(raw)
    assert report.raw == raw
    assert report.path is None


# ---------------------------------------------------------------------------
# Integration with parse_plan_markdown — Files: shape-check applied.
# ---------------------------------------------------------------------------


def test_parse_plan_markdown_drops_paren_hedge_in_files() -> None:
    """An end-to-end smoke test that the Files: site routes entries
    through the helper."""
    from orchestrator.plan_parser import parse_plan_markdown

    md = """# Plan: Demo

## Phase 1: Implement

### Task 1.1: do the thing
  - Description: ok
  - Files: src/foo.py, (and any helper file)
"""
    plan = parse_plan_markdown(md)
    task = plan.phases[0].tasks[0]
    assert task.files == ["src/foo.py"]


def test_parse_plan_markdown_drops_placeholder_in_edit_scope() -> None:
    """An end-to-end smoke test that the EDIT_SCOPE: site routes entries
    through the helper."""
    from orchestrator.plan_parser import parse_plan_markdown

    md = """# Plan: Demo

EDIT_SCOPE:
  - src/math
  - TBD

## Phase 1: Implement

### Task 1.1: real task
  - Description: ok
  - Files: src/foo.py
"""
    plan = parse_plan_markdown(md)
    assert plan.edit_scope == ["src/math"]


def test_parse_plan_markdown_drops_bracket_hedge_in_extended_scope() -> None:
    """An end-to-end smoke test that the Extended-scope: site routes
    entries through the helper."""
    from orchestrator.plan_parser import parse_plan_markdown

    md = """# Plan: Demo

## Phase 1: Implement

### Task 1.1: real task
  - Description: ok
  - Files: src/foo.py
  - Extended-scope: src/bar, [maybe also baz
"""
    plan = parse_plan_markdown(md)
    task = plan.phases[0].tasks[0]
    assert task.extended_scope == ["src/bar"]


def test_parse_plan_markdown_preserves_legitimate_space_with_slash() -> None:
    """REGRESSION GUARD: paths with both a space and a slash are
    legitimate and must NOT be rejected by the shape-check."""
    from orchestrator.plan_parser import parse_plan_markdown

    md = """# Plan: Demo

## Phase 1: Implement

### Task 1.1: update doc
  - Description: ok
  - Files: docs/My File.md
"""
    plan = parse_plan_markdown(md)
    task = plan.phases[0].tasks[0]
    assert task.files == ["docs/My File.md"]
