"""Tests for v0.24.3 :mod:`orchestrator.file_existence_validator`.

The validator runs after :func:`parse_plan_markdown` and rejects plans
whose file/scope paths don't exist on disk. The retry envelope at
:mod:`orchestrator.plan_phase` catches the resulting
:class:`PathValidationError` and feeds the architect a structured hint;
these tests cover the validator surface itself in isolation.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from orchestrator.file_existence_validator import (
    _RepoFileSnapshot,
    validate_files_exist,
)
from orchestrator.path_validator import PathValidationError
from state.schemas import AcceptanceCriterion, Phase, Plan, Task


def _git_init(repo: Path) -> None:
    """Mirrors ``conftest._git_init_repo`` — local copy keeps these tests
    self-contained (no fixture dependency for an isolated module)."""
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=str(repo), check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=str(repo), check=True
    )


def _git_add_and_commit(repo: Path, message: str = "initial") -> None:
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "commit", "-qm", message], cwd=str(repo), check=True
    )


def _make_plan(
    *,
    files: list[str] | None = None,
    files_new: list[str] | None = None,
    extended_scope: list[str] | None = None,
    phase_edit_scope: list[str] | None = None,
    plan_edit_scope: list[str] | None = None,
) -> Plan:
    """Build a minimal Plan with one phase + one task; everything else default."""
    task = Task(
        id="1.1",
        phase_id="1",
        title="t",
        description="d",
        files=files or [],
        files_new=files_new or [],
        extended_scope=extended_scope or [],
        acceptance=[
            AcceptanceCriterion(id="ac-1", description="passes")
        ],
    )
    phase = Phase(
        id="1",
        title="p",
        description="d",
        tasks=[task],
        edit_scope=phase_edit_scope,
    )
    return Plan(
        plan_id="plan-test",
        spec_hash="deadbeef",
        phases=[phase],
        edit_scope=plan_edit_scope or [],
        created_at="2026-05-10T00:00:00+00:00",
        updated_at="2026-05-10T00:00:00+00:00",
    )


def test_all_files_exist_passes(tmp_path: Path) -> None:
    """A plan referencing only real, tracked files returns ``None`` and does
    not raise."""
    _git_init(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.cpp").write_text("// foo\n", encoding="utf-8")
    (tmp_path / "src" / "bar.h").write_text("// bar\n", encoding="utf-8")
    _git_add_and_commit(tmp_path)

    plan = _make_plan(files=["src/foo.cpp", "src/bar.h"])
    # No raise → success.
    assert validate_files_exist(plan, tmp_path) is None


def test_one_missing_file_raises_with_path(tmp_path: Path) -> None:
    """The first missing file raises ``PathValidationError`` carrying the
    raw path and ``reason="missing_on_disk"``."""
    _git_init(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.cpp").write_text("// foo\n", encoding="utf-8")
    _git_add_and_commit(tmp_path)

    plan = _make_plan(files=["src/foo.cpp", "src/imaginary.cpp"])
    with pytest.raises(PathValidationError) as excinfo:
        validate_files_exist(plan, tmp_path)
    assert excinfo.value.raw == "src/imaginary.cpp"
    assert excinfo.value.reason == "missing_on_disk"


def test_missing_file_includes_fuzzy_suggestion(tmp_path: Path) -> None:
    """``difflib`` close-match should suggest the real file when the typo is
    a single character away."""
    _git_init(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.cpp").write_text("// foo\n", encoding="utf-8")
    _git_add_and_commit(tmp_path)

    plan = _make_plan(files=["src/foo.cp"])  # missing trailing 'p'
    with pytest.raises(PathValidationError) as excinfo:
        validate_files_exist(plan, tmp_path)
    assert excinfo.value.suggestion == "src/foo.cpp"


def test_new_prefix_files_skipped(tmp_path: Path) -> None:
    """Paths in ``Task.files_new`` are NOT subjected to the existence check —
    that's the v0.24.3 opt-out for files the task itself will create."""
    _git_init(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.cpp").write_text("// foo\n", encoding="utf-8")
    _git_add_and_commit(tmp_path)

    plan = _make_plan(
        files=["src/about_to_be_created.cpp"],
        files_new=["src/about_to_be_created.cpp"],
    )
    # No raise — the path is opt-out via files_new.
    assert validate_files_exist(plan, tmp_path) is None


def test_extended_scope_dir_prefix_validated(tmp_path: Path) -> None:
    """A nonexistent directory prefix in ``Task.extended_scope`` raises."""
    _git_init(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.cpp").write_text("// foo\n", encoding="utf-8")
    _git_add_and_commit(tmp_path)

    plan = _make_plan(
        files=["src/foo.cpp"], extended_scope=["src/nonexistent_dir"]
    )
    with pytest.raises(PathValidationError) as excinfo:
        validate_files_exist(plan, tmp_path)
    assert excinfo.value.raw == "src/nonexistent_dir"
    assert excinfo.value.reason == "missing_on_disk"


def test_extended_scope_with_existing_dir_passes(tmp_path: Path) -> None:
    """Any tracked file under the prefix counts — ``extended_scope=["src/qa"]``
    passes when ``src/qa/foo.py`` is tracked."""
    _git_init(tmp_path)
    (tmp_path / "src" / "qa").mkdir(parents=True)
    (tmp_path / "src" / "qa" / "foo.py").write_text("# foo\n", encoding="utf-8")
    _git_add_and_commit(tmp_path)

    plan = _make_plan(
        files=["src/qa/foo.py"], extended_scope=["src/qa"]
    )
    assert validate_files_exist(plan, tmp_path) is None


def test_phase_edit_scope_validated(tmp_path: Path) -> None:
    """Phase-level ``edit_scope=["src/missing"]`` raises."""
    _git_init(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.cpp").write_text("// foo\n", encoding="utf-8")
    _git_add_and_commit(tmp_path)

    plan = _make_plan(
        files=["src/foo.cpp"], phase_edit_scope=["src/missing"]
    )
    with pytest.raises(PathValidationError) as excinfo:
        validate_files_exist(plan, tmp_path)
    assert excinfo.value.raw == "src/missing"
    assert excinfo.value.reason == "missing_on_disk"


def test_plan_edit_scope_validated(tmp_path: Path) -> None:
    """Plan-level ``edit_scope=["src/missing"]`` raises."""
    _git_init(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.cpp").write_text("// foo\n", encoding="utf-8")
    _git_add_and_commit(tmp_path)

    plan = _make_plan(
        files=["src/foo.cpp"], plan_edit_scope=["src/missing"]
    )
    with pytest.raises(PathValidationError) as excinfo:
        validate_files_exist(plan, tmp_path)
    assert excinfo.value.raw == "src/missing"
    assert excinfo.value.reason == "missing_on_disk"


def test_path_traversal_already_rejected_by_schema(tmp_path: Path) -> None:
    """Sanity: ``..`` paths still die at schema validation, not the new
    file-existence validator. ``Task(files=["../escape"])`` raises a
    pydantic ``ValidationError`` BEFORE we ever reach ``validate_files_exist``.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Task(
            id="1.1",
            phase_id="1",
            title="t",
            description="d",
            files=["../escape"],
            acceptance=[],
        )


def test_snapshot_caches_one_subprocess_call(tmp_path: Path) -> None:
    """Three lookups against the same ``_RepoFileSnapshot`` instance must
    only invoke ``subprocess.run`` once — the snapshot is built lazily on
    first access and cached for the lifetime of the instance."""
    _git_init(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.cpp").write_text("// foo\n", encoding="utf-8")
    _git_add_and_commit(tmp_path)

    snapshot = _RepoFileSnapshot.for_cwd(tmp_path)
    real_run = subprocess.run
    with mock.patch(
        "orchestrator.file_existence_validator.subprocess.run",
        side_effect=real_run,
    ) as mock_run:
        snapshot.exists("src/foo.cpp")
        snapshot.exists("src/bar.cpp")
        snapshot.is_dir_prefix("src")
        snapshot.closest("src/imaginary.cpp")
    assert mock_run.call_count == 1
