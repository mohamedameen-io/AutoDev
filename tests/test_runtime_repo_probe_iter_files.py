"""Tests for :func:`runtime.repo_probe.iter_repo_files` (v0.25.0)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from runtime.repo_probe import iter_repo_files


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=str(repo), check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=str(repo), check=True
    )


def _git_commit(repo: Path) -> None:
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "commit", "-qm", "init"], cwd=str(repo), check=True
    )


def test_iter_repo_files_yields_tracked_files(tmp_path: Path) -> None:
    """Git fast-path: yields tracked files only, untracked files excluded."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    (repo / "tracked.py").write_text("x = 1\n")
    (repo / "tracked.cpp").write_text("// hi\n")
    _git_commit(repo)
    (repo / "untracked.py").write_text("y = 2\n")  # NOT committed

    out = sorted(p.name for p in iter_repo_files(repo))
    assert "tracked.py" in out
    assert "tracked.cpp" in out
    assert "untracked.py" not in out


def test_iter_repo_files_filters_by_extension(tmp_path: Path) -> None:
    """Extension allowlist drops files whose suffix is not in the set."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    (repo / "a.py").write_text("x = 1\n")
    (repo / "b.cpp").write_text("// hi\n")
    (repo / "c.txt").write_text("readme\n")
    _git_commit(repo)

    out = sorted(
        p.name for p in iter_repo_files(repo, extensions=frozenset({".py"}))
    )
    assert out == ["a.py"]


def test_iter_repo_files_skips_skip_dirs(tmp_path: Path) -> None:
    """Walk fallback skips canonical noise dirs (.git, node_modules, etc.)."""
    repo = tmp_path / "r"
    repo.mkdir()
    # Intentionally NO git init — exercise the os.walk fallback path.
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("x = 1\n")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "junk.js").write_text("x;\n")
    (repo / ".venv").mkdir()
    (repo / ".venv" / "boot.py").write_text("x = 1\n")
    (repo / "build").mkdir()
    (repo / "build" / "out.o").write_text("\n")

    out = sorted(p.name for p in iter_repo_files(repo))
    assert "a.py" in out
    assert "junk.js" not in out  # node_modules
    assert "boot.py" not in out  # .venv
    assert "out.o" not in out  # build
