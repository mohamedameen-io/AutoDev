"""v0.17.0 S6: ``WorktreeManager.create`` accepts ``sparse_paths``.

When ``sparse_paths`` is set, the worktree is created with
``git worktree add --no-checkout``, configured for cone-mode sparse-checkout
via ``sparse-checkout init --cone``, narrowed to the given prefixes via
``sparse-checkout set``, and finally checked out (so files in the sparse
set materialize on disk).

Pre-flight: requires git ≥2.25 (cone-mode landed there). On older git,
falls back to a full checkout with a warning.

These tests use real git subprocesses to integrate-test against the
git binary on the test machine.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orchestrator.worktree import WorktreeManager


def _build_repo(repo: Path, files: dict[str, str]) -> None:
    """Initialise a git repo with the given files (path -> content)."""
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=repo, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=repo, check=True
    )
    for rel, content in files.items():
        full = repo / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"],
        cwd=repo,
        check=True,
    )


@pytest.mark.asyncio
async def test_sparse_checkout_narrows_files(tmp_path: Path) -> None:
    """``sparse_paths=['src']`` materializes only files under ``src/``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _build_repo(
        repo,
        {
            "src/qa/foo.py": "x\n",
            "src/main.py": "y\n",
            "tests/test_foo.py": "z\n",
            "scripts/util.sh": "#!/bin/sh\n",
        },
    )

    mgr = WorktreeManager(repo, tmp_path / "tournaments")
    wt = await mgr.create("a", sparse_paths=["src"])

    # After sparse checkout, src/ files exist but tests/ + scripts/ don't.
    assert (wt / "src" / "qa" / "foo.py").exists()
    assert (wt / "src" / "main.py").exists()
    assert not (wt / "tests" / "test_foo.py").exists()
    assert not (wt / "scripts" / "util.sh").exists()


@pytest.mark.asyncio
async def test_sparse_checkout_none_falls_back_to_full(tmp_path: Path) -> None:
    """``sparse_paths=None`` uses the legacy full-checkout path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _build_repo(
        repo,
        {
            "src/qa/foo.py": "x\n",
            "tests/test_foo.py": "z\n",
        },
    )

    mgr = WorktreeManager(repo, tmp_path / "tournaments")
    wt = await mgr.create("a")  # no sparse_paths kwarg

    # Full checkout: every tracked file present.
    assert (wt / "src" / "qa" / "foo.py").exists()
    assert (wt / "tests" / "test_foo.py").exists()


@pytest.mark.asyncio
async def test_sparse_checkout_multiple_paths(tmp_path: Path) -> None:
    """Multiple prefix entries are all included."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _build_repo(
        repo,
        {
            "src/main.py": "y\n",
            "tests/test_a.py": "z\n",
            "scripts/x.sh": "x\n",
        },
    )

    mgr = WorktreeManager(repo, tmp_path / "tournaments")
    wt = await mgr.create("a", sparse_paths=["src", "tests"])

    assert (wt / "src" / "main.py").exists()
    assert (wt / "tests" / "test_a.py").exists()
    # scripts/ NOT included.
    assert not (wt / "scripts" / "x.sh").exists()


@pytest.mark.asyncio
async def test_sparse_paths_empty_list_full_checkout(tmp_path: Path) -> None:
    """``sparse_paths=[]`` is treated as None → full checkout (defensive)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _build_repo(
        repo,
        {
            "src/main.py": "y\n",
            "tests/test_a.py": "z\n",
        },
    )

    mgr = WorktreeManager(repo, tmp_path / "tournaments")
    wt = await mgr.create("a", sparse_paths=[])

    assert (wt / "src" / "main.py").exists()
    assert (wt / "tests" / "test_a.py").exists()


@pytest.mark.asyncio
async def test_sparse_checkout_old_git_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-flight check: git <2.25 falls back to full checkout with warning."""
    from orchestrator import worktree as wt_mod

    async def fake_git_version(*_: object) -> tuple[int, int, int]:
        return (2, 24, 0)  # below cone-mode threshold

    monkeypatch.setattr(wt_mod, "_get_git_version", fake_git_version)

    repo = tmp_path / "repo"
    repo.mkdir()
    _build_repo(
        repo,
        {
            "src/main.py": "y\n",
            "tests/test_a.py": "z\n",
        },
    )

    mgr = WorktreeManager(repo, tmp_path / "tournaments")
    wt = await mgr.create("a", sparse_paths=["src"])

    # Fallback: full checkout — both files materialize.
    assert (wt / "src" / "main.py").exists()
    assert (wt / "tests" / "test_a.py").exists()
