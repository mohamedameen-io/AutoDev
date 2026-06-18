"""F-6 (Fix 2): tracked test-harness files in the per-task sparse cone.

The QA ``test_runner`` gate runs in the per-task worktree (cwd=worktree). The
sparse cone (``edit_scope`` else ``task.files`` + sibling C/C++ headers) used to
check out ONLY those paths, OMITTING build/test-harness files (``package.json``,
lockfiles, ``pyproject.toml``, ``conftest.py``, the project's test files). The
gate then could not run tests on huge repos.

:meth:`WorktreeManager.create_per_task` now folds a small, curated, TRACKED-ONLY
globset of harness files into the sparse cone (behind
``worktree_sparse_include_harness``, default True), bounded by
:data:`WORKTREE_HEADER_EXPANSION_CAP` so the cone stays sparse-for-scale.

These tests use real git on a tmp repo (the harness inclusion materializes real
files on disk), matching the rest of the worktree integration suite.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestrator.worktree import (
    WORKTREE_HEADER_EXPANSION_CAP,
    WorktreeManager,
    _harness_paths_for_sparse,
)


def _init_git_repo_with(path: Path, files: dict[str, str]) -> None:
    """Initialise a git repo at *path* with the given files (path -> content)."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"],
        cwd=str(path), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "t"],
        cwd=str(path), check=True, capture_output=True,
    )
    for rel, body in files.items():
        fp = path / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(body)
    subprocess.run(["git", "add", "-A"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "seed"],
        cwd=str(path), check=True, capture_output=True,
    )


@pytest.mark.asyncio
async def test_nodejs_harness_materializes_in_sparse_cone(tmp_path: Path) -> None:
    """RED→GREEN: package.json + the package's test file join a src/foo.js cone.

    Before the fix, ``sparse_paths=["src/foo.js"]`` materialized ONLY
    ``src/foo.js`` — the QA gate's ``npm test`` could not see ``package.json``
    or the test file. The harness inclusion now pulls both into the cone.
    """
    repo = tmp_path / "repo"
    _init_git_repo_with(
        repo,
        {
            "package.json": '{"scripts": {"test": "jest"}}\n',
            "package-lock.json": "{}\n",
            "src/foo.js": "module.exports = () => 1;\n",
            "test/foo.test.js": "test('x', () => {});\n",
            # An unrelated file that must NOT be dragged in by the cone.
            "docs/guide.md": "# guide\n",
        },
    )
    mgr = WorktreeManager(main_repo=repo, tournament_dir=tmp_path / "wts")
    wt = await mgr.create_per_task("1.1", sparse_paths=["src/foo.js"])

    # The requested source file is present (baseline cone behavior).
    assert (wt / "src" / "foo.js").exists()
    # Harness files are folded in so the QA gate can run.
    assert (wt / "package.json").exists()
    assert (wt / "package-lock.json").exists()
    # The test file (under the task's package tree) materializes too.
    assert (wt / "test" / "foo.test.js").exists()
    # Unrelated content is still excluded — the cone stays narrow.
    assert not (wt / "docs" / "guide.md").exists()

    await mgr.cleanup_all()


@pytest.mark.asyncio
async def test_python_harness_materializes_in_sparse_cone(tmp_path: Path) -> None:
    """Python: pyproject.toml, conftest.py, and the package's tests join the cone."""
    repo = tmp_path / "repo"
    _init_git_repo_with(
        repo,
        {
            "pyproject.toml": "[project]\nname='x'\n",
            "pytest.ini": "[pytest]\n",
            "conftest.py": "# root conftest\n",
            "pkg/conftest.py": "# pkg conftest\n",
            "pkg/foo.py": "def f():\n    return 1\n",
            "pkg/tests/test_foo.py": "def test_f():\n    assert True\n",
            "unrelated/huge.py": "# noise\n",
        },
    )
    mgr = WorktreeManager(main_repo=repo, tournament_dir=tmp_path / "wts")
    wt = await mgr.create_per_task("1.1", sparse_paths=["pkg/foo.py"])

    assert (wt / "pkg" / "foo.py").exists()
    # Root + ancestor harness manifests/conftests.
    assert (wt / "pyproject.toml").exists()
    assert (wt / "pytest.ini").exists()
    assert (wt / "conftest.py").exists()
    assert (wt / "pkg" / "conftest.py").exists()
    # The package's own tests come along (scoped to the task's dir tree).
    assert (wt / "pkg" / "tests" / "test_foo.py").exists()
    # A test tree OUTSIDE the task's package is not pulled in.
    assert not (wt / "unrelated" / "huge.py").exists()

    await mgr.cleanup_all()


@pytest.mark.asyncio
async def test_harness_inclusion_disabled_preserves_scope_only(tmp_path: Path) -> None:
    """``worktree_sparse_include_harness=False`` → only the requested scope."""
    repo = tmp_path / "repo"
    _init_git_repo_with(
        repo,
        {
            "package.json": '{"scripts": {"test": "jest"}}\n',
            "src/foo.js": "module.exports = () => 1;\n",
            "test/foo.test.js": "test('x', () => {});\n",
        },
    )
    mgr = WorktreeManager(main_repo=repo, tournament_dir=tmp_path / "wts")
    wt = await mgr.create_per_task(
        "1.1",
        sparse_paths=["src/foo.js"],
        include_harness_for_sparse=False,
    )

    assert (wt / "src" / "foo.js").exists()
    assert mgr.last_sparse_harness_added == 0
    # Flag off → the OTHER-directory test file is NOT folded in (legacy
    # scope-only behavior). NB: ``package.json`` is a repo-ROOT file and git
    # cone-mode always materializes root-level files regardless of the cone
    # set, so it is NOT a discriminating assertion here — the test file under
    # ``test/`` is the path that genuinely depends on harness inclusion.
    assert not (wt / "test" / "foo.test.js").exists()

    await mgr.cleanup_all()


@pytest.mark.asyncio
async def test_harness_inclusion_capped_bails_gracefully(tmp_path: Path) -> None:
    """When harness expansion exceeds the cap, it bails (cone stays bounded).

    We force the harness resolver to propose more than
    ``WORKTREE_HEADER_EXPANSION_CAP`` paths. The cone must NOT balloon: the
    harness inclusion is skipped entirely and only the requested scope (plus
    any in-cap sibling headers) materializes — no crash.
    """
    repo = tmp_path / "repo"
    _init_git_repo_with(
        repo,
        {
            "package.json": '{"scripts": {"test": "jest"}}\n',
            "src/foo.js": "module.exports = () => 1;\n",
            "test/foo.test.js": "test('x', () => {});\n",
        },
    )
    mgr = WorktreeManager(main_repo=repo, tournament_dir=tmp_path / "wts")

    over_cap = [f"harness_{i}.json" for i in range(WORKTREE_HEADER_EXPANSION_CAP + 5)]
    with patch(
        "orchestrator.worktree._harness_paths_for_sparse",
        return_value=set(over_cap),
    ):
        wt = await mgr.create_per_task("1.2", sparse_paths=["src/foo.js"])

    # Cap tripped → no harness paths added (counter stays 0), cone bounded.
    assert mgr.last_sparse_harness_added == 0
    assert (wt / "src" / "foo.js").exists()
    # The OTHER-directory test file was in the (capped-out) harness set → NOT
    # materialized; the cone stayed bounded (no crash). NB: ``package.json`` is
    # a repo-ROOT file and is always present in cone mode, so it is not a
    # discriminating assertion here.
    assert not (wt / "test" / "foo.test.js").exists()

    await mgr.cleanup_all()


def test_root_scoped_task_skips_root_leaf_scan(tmp_path: Path) -> None:
    """Root-scoped task: leaf-dir ``git ls-files "*"`` is NOT issued.

    When the task's scope file lives at the repo root (e.g. ``"setup.py"``),
    the leaf dir is ``""`` and the pathspec would be ``"*"`` — which git
    resolves RECURSIVELY across the entire repo, exhausting memory on a
    million-file monorepo before the cap bail ever fires.

    The fix skips that scan entirely when ``leaf dir == ""``. Root
    manifests/conftest are still pulled by the ancestor scan; root-level
    co-located test files (unusual in practice) are the only thing skipped.

    We verify by patching ``subprocess_run_ls_files`` so we can assert that
    NO call with pathspec ``"*"`` is issued, while the ancestor manifest
    calls still happen (``pyproject.toml``, etc.).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    # We don't need a real git repo — we mock the ls-files helper entirely.
    calls: list[str] = []

    def _fake_ls_files(git_root: Path, pathspec: str) -> str:
        calls.append(pathspec)
        # Return a fake hit for the root pyproject.toml so we can confirm the
        # ancestor scan ran.
        if pathspec == "pyproject.toml":
            return "pyproject.toml\n"
        return ""

    with patch(
        "orchestrator.worktree.subprocess_run_ls_files",
        side_effect=_fake_ls_files,
    ):
        result = _harness_paths_for_sparse(repo, ["setup.py"])

    # The root manifest scan ran (confirming the ancestor path is active).
    assert "pyproject.toml" in calls
    # The root ``"*"`` leaf-dir scan was SKIPPED (no repo-wide listing).
    assert "*" not in calls, (
        f"repo-wide 'git ls-files *' was issued; calls = {calls!r}"
    )
    # Root pyproject.toml is admitted (from the ancestor scan).
    assert "pyproject.toml" in result
    # A deep unrelated test file is NOT in the result (it was never scanned).
    assert "deep/nested/test_something.py" not in result
