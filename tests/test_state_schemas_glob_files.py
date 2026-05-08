"""v0.17.0 S5: ``Task.files`` accepts glob patterns.

The schema validator should accept glob entries (with ``*``, ``?``,
``[...]`` characters) without raising. Callers (``find_file_overlaps``,
``validate_edit_scope``) handle expansion against a tracked-files cache.

This test pins the validator surface so future field-validator changes
don't accidentally reject globs.
"""

from __future__ import annotations

from state.schemas import Task


def test_task_files_accepts_glob_star() -> None:
    t = Task(
        id="1.1",
        phase_id="1",
        title="t",
        description="d",
        files=["src/qa/*.py"],
    )
    assert t.files == ["src/qa/*.py"]


def test_task_files_accepts_glob_double_star() -> None:
    t = Task(
        id="1.1",
        phase_id="1",
        title="t",
        description="d",
        files=["**/*.py"],
    )
    assert t.files == ["**/*.py"]


def test_task_files_accepts_glob_question_mark() -> None:
    t = Task(
        id="1.1",
        phase_id="1",
        title="t",
        description="d",
        files=["src/file?.py"],
    )
    assert t.files == ["src/file?.py"]


def test_task_files_accepts_glob_charclass() -> None:
    t = Task(
        id="1.1",
        phase_id="1",
        title="t",
        description="d",
        files=["src/file[ab].py"],
    )
    assert t.files == ["src/file[ab].py"]


def test_task_files_accepts_mix_of_globs_and_explicit() -> None:
    t = Task(
        id="1.1",
        phase_id="1",
        title="t",
        description="d",
        files=["src/qa/*.py", "src/main.py"],
    )
    assert t.files == ["src/qa/*.py", "src/main.py"]


def test_task_files_rejects_absolute_path() -> None:
    """Absolute paths are repo-escaping — explicitly rejected."""
    import pytest

    with pytest.raises(ValueError, match="repo-relative"):
        Task(
            id="1.1",
            phase_id="1",
            title="t",
            description="d",
            files=["/etc/passwd"],
        )


def test_task_files_rejects_dotdot_segment() -> None:
    """``..`` parent-traversal segments are explicitly rejected."""
    import pytest

    with pytest.raises(ValueError, match=r"\.\."):
        Task(
            id="1.1",
            phase_id="1",
            title="t",
            description="d",
            files=["src/../etc/passwd"],
        )


def test_task_files_rejects_empty_string_entry() -> None:
    import pytest

    with pytest.raises(ValueError, match="non-empty"):
        Task(
            id="1.1",
            phase_id="1",
            title="t",
            description="d",
            files=[""],
        )
