"""v0.20.0 C1: Task.extended_scope schema validation."""

from __future__ import annotations

import pytest

from state.schemas import Task


def _make_task(**overrides: object) -> Task:
    base = dict(
        id="1.1",
        phase_id="1",
        title="t",
        description="d",
        files=[],
    )
    base.update(overrides)
    return Task.model_validate(base)


def test_default_extended_scope_is_empty_list() -> None:
    t = _make_task()
    assert t.extended_scope == []


def test_extended_scope_accepts_simple_paths() -> None:
    t = _make_task(extended_scope=["src/foo", "tests/foo"])
    assert t.extended_scope == ["src/foo", "tests/foo"]


def test_extended_scope_trims_trailing_slashes() -> None:
    t = _make_task(extended_scope=["src/foo/", "tests/foo/"])
    assert t.extended_scope == ["src/foo", "tests/foo"]


def test_extended_scope_rejects_absolute_path() -> None:
    with pytest.raises(Exception):
        _make_task(extended_scope=["/etc/passwd"])


def test_extended_scope_rejects_parent_dir_segment() -> None:
    with pytest.raises(Exception):
        _make_task(extended_scope=["src/../leak"])


def test_extended_scope_allows_dotted_filename_substring() -> None:
    """``..`` rejected only as a whole segment, not as a substring."""
    t = _make_task(extended_scope=["src/some..file"])
    assert t.extended_scope == ["src/some..file"]


def test_extended_scope_rejects_non_string_entries() -> None:
    with pytest.raises(Exception):
        _make_task(extended_scope=[123])  # type: ignore[list-item]
