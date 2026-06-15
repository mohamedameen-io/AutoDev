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


def _make_two_task_plan(
    *,
    task_a_files: list[str] | None = None,
    task_a_files_new: list[str] | None = None,
    task_b_files: list[str] | None = None,
    task_b_files_new: list[str] | None = None,
) -> Plan:
    """v0.33.0 A1 fixture: two tasks across one phase. Used to exercise the
    plan-global ``[new]`` union behaviour where one task declares the file
    and a later task references it without ``[new]``."""
    task_a = Task(
        id="1.1",
        phase_id="1",
        title="creator",
        description="d",
        files=task_a_files or [],
        files_new=task_a_files_new or [],
        acceptance=[AcceptanceCriterion(id="ac-a", description="passes")],
    )
    task_b = Task(
        id="3.1",
        phase_id="1",
        title="consumer",
        description="d",
        files=task_b_files or [],
        files_new=task_b_files_new or [],
        acceptance=[AcceptanceCriterion(id="ac-b", description="passes")],
    )
    phase = Phase(
        id="1",
        title="p",
        description="d",
        tasks=[task_a, task_b],
        edit_scope=None,
    )
    return Plan(
        plan_id="plan-two-task",
        spec_hash="deadbeef",
        phases=[phase],
        edit_scope=[],
        created_at="2026-05-16T00:00:00+00:00",
        updated_at="2026-05-16T00:00:00+00:00",
    )


def test_validate_files_exist_plan_global_new(tmp_path: Path) -> None:
    """v0.33.0 A1: task A declares ``new_artifact.md`` as ``[new]``; task B
    references the same path with no ``[new]`` prefix. Validation passes
    because the path is admitted via the plan-global ``files_new`` union."""
    _git_init(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.cpp").write_text("// foo\n", encoding="utf-8")
    _git_add_and_commit(tmp_path)

    plan = _make_two_task_plan(
        task_a_files=["new_artifact.md"],
        task_a_files_new=["new_artifact.md"],
        task_b_files=["new_artifact.md"],
    )
    resolutions: list[dict[str, str]] = []
    assert validate_files_exist(plan, tmp_path, resolutions=resolutions) is None
    # task A's reference resolves trivially via its own files_new (not
    # recorded as a plan-global admission); task B's resolution flows
    # through the new path and surfaces in the out-channel.
    assert len(resolutions) == 1
    assert resolutions[0]["task_id"] == "3.1"
    assert resolutions[0]["path"] == "new_artifact.md"
    assert resolutions[0]["declaring_task_id"] == "1.1"


def test_validate_files_exist_plan_global_new_normalizes_paths(
    tmp_path: Path,
) -> None:
    """v0.33.0 A1: declared path with a trailing slash collapses to the
    same key as the consumer's bare reference. ``_normalize_path_entry``
    is the canonical hop on both sides — it strips trailing slashes and
    inline ``# comment`` tails, so the union lookup matches even when
    the architect emits a stylistic variant."""
    _git_init(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.cpp").write_text("// foo\n", encoding="utf-8")
    _git_add_and_commit(tmp_path)

    plan = _make_two_task_plan(
        task_a_files_new=["new_artifact.md/"],
        task_b_files=["new_artifact.md"],
    )
    # No raise — both sides canonicalise to "new_artifact.md".
    assert validate_files_exist(plan, tmp_path) is None


def test_missing_on_disk_raises_when_no_task_declares_new(
    tmp_path: Path,
) -> None:
    """v0.33.0 A1 negative case: neither task declares ``new_artifact.md``
    via ``[new]``. The plan-global union is empty for this path, so the
    on-disk existence check fires and ``PathValidationError`` raises."""
    _git_init(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.cpp").write_text("// foo\n", encoding="utf-8")
    _git_add_and_commit(tmp_path)

    plan = _make_two_task_plan(
        task_a_files=["src/foo.cpp"],
        task_b_files=["new_artifact.md"],
    )
    with pytest.raises(PathValidationError) as excinfo:
        validate_files_exist(plan, tmp_path)
    assert excinfo.value.raw == "new_artifact.md"
    assert excinfo.value.reason == "missing_on_disk"


# ---------------------------------------------------------------------------
# v0.40.1: additive real-filesystem fallback. The git ls-files snapshot is a
# tracked-only view; a path that exists on disk but is gitignored / uncommitted
# (or a file-shaped scope entry) must no longer be false-rejected.
# ---------------------------------------------------------------------------


def test_task_files_on_disk_but_untracked_passes(tmp_path: Path) -> None:
    """A ``Task.files`` path that exists on disk but is NOT git-tracked
    (gitignored) is admitted via the on-disk fallback — no
    ``PathValidationError``.

    The snapshot is non-empty (``src/foo.cpp`` is tracked, so the empty-
    snapshot short-circuit does not fire), yet ``.env.example`` is absent
    from ``git ls-files`` because ``.gitignore`` excludes it. It exists on
    disk, so the fallback accepts it."""
    _git_init(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.cpp").write_text("// foo\n", encoding="utf-8")
    # Gitignore .env.example so it is real-on-disk but untracked.
    (tmp_path / ".gitignore").write_text(".env.example\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("KEY=value\n", encoding="utf-8")
    _git_add_and_commit(tmp_path)

    # Sanity: the snapshot genuinely does not list the untracked file.
    snapshot = _RepoFileSnapshot.for_cwd(tmp_path)
    assert not snapshot.exists(".env.example")
    assert snapshot.exists_on_disk(".env.example")

    plan = _make_plan(files=["src/foo.cpp", ".env.example"])
    assert validate_files_exist(plan, tmp_path) is None


def test_task_files_on_disk_fallback_does_not_record_resolution(
    tmp_path: Path,
) -> None:
    """An on-disk-but-untracked ``Task.files`` admission is a *present*
    file, not a ``[new]`` one — it must NOT append to the resolutions
    out-channel (that ledger op is reserved for plan-global ``[new]``
    admissions)."""
    _git_init(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.cpp").write_text("// foo\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(".env.example\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("KEY=value\n", encoding="utf-8")
    _git_add_and_commit(tmp_path)

    plan = _make_plan(files=[".env.example"])
    resolutions: list[dict[str, str]] = []
    assert validate_files_exist(plan, tmp_path, resolutions=resolutions) is None
    assert resolutions == []


def test_plan_edit_scope_file_path_on_disk_passes(tmp_path: Path) -> None:
    """A FILE path (not a directory) in ``plan.edit_scope`` is accepted when
    it exists on disk. The dir-prefix check structurally fails for a file
    (no tracked path starts with ``pyproject.toml/``); the on-disk scope
    fallback admits it as the architect declaring intent to edit that
    existing file."""
    _git_init(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.cpp").write_text("// foo\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'x'\n", encoding="utf-8"
    )
    _git_add_and_commit(tmp_path)

    # ``pyproject.toml`` IS tracked here, but it is a file — is_dir_prefix
    # returns False for it. The scope fallback (file-or-dir) is what admits
    # it. (The bug report's pyproject.toml was tracked yet still rejected
    # precisely because of this file-vs-dir-prefix mismatch.)
    snapshot = _RepoFileSnapshot.for_cwd(tmp_path)
    assert not snapshot.is_dir_prefix("pyproject.toml")
    assert snapshot.scope_exists_on_disk("pyproject.toml")

    plan = _make_plan(files=["src/foo.cpp"], plan_edit_scope=["pyproject.toml"])
    assert validate_files_exist(plan, tmp_path) is None


def test_extended_scope_file_path_on_disk_passes(tmp_path: Path) -> None:
    """The on-disk scope fallback also applies to ``Task.extended_scope``
    file-shaped entries."""
    _git_init(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.cpp").write_text("// foo\n", encoding="utf-8")
    (tmp_path / "Makefile").write_text("all:\n\t@true\n", encoding="utf-8")
    _git_add_and_commit(tmp_path)

    plan = _make_plan(files=["src/foo.cpp"], extended_scope=["Makefile"])
    assert validate_files_exist(plan, tmp_path) is None


def test_scope_dir_with_only_untracked_files_passes(tmp_path: Path) -> None:
    """A real directory containing only gitignored files carries no tracked
    ``dir/`` prefix, so ``is_dir_prefix`` is False — but it exists on disk,
    so the scope fallback admits it."""
    _git_init(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.cpp").write_text("// foo\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("build/\n", encoding="utf-8")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "artifact.o").write_text("x\n", encoding="utf-8")
    _git_add_and_commit(tmp_path)

    snapshot = _RepoFileSnapshot.for_cwd(tmp_path)
    assert not snapshot.is_dir_prefix("build")
    assert snapshot.scope_exists_on_disk("build")

    plan = _make_plan(files=["src/foo.cpp"], plan_edit_scope=["build"])
    assert validate_files_exist(plan, tmp_path) is None


def test_genuinely_missing_path_still_raises_after_fallback(
    tmp_path: Path,
) -> None:
    """Regression guard: a path absent from BOTH the tracked snapshot AND
    the real filesystem still raises ``PathValidationError`` — the fallback
    loosens acceptance but never suppresses a genuine miss."""
    _git_init(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.cpp").write_text("// foo\n", encoding="utf-8")
    _git_add_and_commit(tmp_path)

    plan = _make_plan(files=["src/foo.cpp", "src/does_not_exist.cpp"])
    with pytest.raises(PathValidationError) as excinfo:
        validate_files_exist(plan, tmp_path)
    assert excinfo.value.raw == "src/does_not_exist.cpp"
    assert excinfo.value.reason == "missing_on_disk"


def test_genuinely_missing_scope_still_raises_after_fallback(
    tmp_path: Path,
) -> None:
    """Regression guard for the ``*_scope`` path: a scope entry that is
    neither a tracked-dir prefix nor present on disk still raises."""
    _git_init(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.cpp").write_text("// foo\n", encoding="utf-8")
    _git_add_and_commit(tmp_path)

    plan = _make_plan(
        files=["src/foo.cpp"], plan_edit_scope=["totally/absent/dir"]
    )
    with pytest.raises(PathValidationError) as excinfo:
        validate_files_exist(plan, tmp_path)
    assert excinfo.value.raw == "totally/absent/dir"
    assert excinfo.value.reason == "missing_on_disk"


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


# ---------------------------------------------------------------------------
# v0.36.0 D2: low-quality "did you mean" hint suppression.
# ---------------------------------------------------------------------------


def test_closest_suppresses_unrelated_top_level_dir_hint(tmp_path: Path) -> None:
    """A rejection in one top-level dir must not surface a hint pointing
    at a completely different subtree. Pre-D2 the validator could return
    a path under ``.claude/skills/`` for the rejected token ``notes``."""
    _git_init(tmp_path)
    (tmp_path / ".claude" / "skills").mkdir(parents=True)
    (tmp_path / ".claude" / "skills" / "notes_helper.md").write_text(
        "x\n", encoding="utf-8"
    )
    _git_add_and_commit(tmp_path)

    snapshot = _RepoFileSnapshot.for_cwd(tmp_path)
    # ``notes`` (rejected) lives in a different top-level dir than the
    # only tracked file (under ``.claude/``); the hint should be None.
    assert snapshot.closest("notes") is None


def test_closest_returns_none_for_short_path_low_similarity(tmp_path: Path) -> None:
    """Short rejected paths with low similarity to the best candidate
    get suppressed rather than mis-suggested."""
    _git_init(tmp_path)
    (tmp_path / "abc.py").write_text("# x\n", encoding="utf-8")
    _git_add_and_commit(tmp_path)
    snapshot = _RepoFileSnapshot.for_cwd(tmp_path)
    # ``xyz`` is short AND nothing like ``abc.py`` — D2 suppresses.
    assert snapshot.closest("xyz") is None


def test_closest_returns_hint_when_in_same_subtree(tmp_path: Path) -> None:
    """The plausibility gate does not suppress legitimate intra-subtree
    suggestions."""
    _git_init(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foobar.py").write_text("# x\n", encoding="utf-8")
    _git_add_and_commit(tmp_path)
    snapshot = _RepoFileSnapshot.for_cwd(tmp_path)
    # ``src/foobaz.py`` is a plausible typo of ``src/foobar.py``.
    suggestion = snapshot.closest("src/foobaz.py")
    assert suggestion == "src/foobar.py"
