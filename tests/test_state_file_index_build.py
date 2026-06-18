"""Tests for :class:`state.file_index.IndexBuilder` (v0.25.0)."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from state.file_index import (
    INDEX_SCHEMA_VERSION,
    IndexBuilder,
    IndexBuildContentionError,
    _last_indexed_sha,
    _state_path,
    _lock_path,
)
from state.paths import index_db_path


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=str(repo), check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=str(repo), check=True
    )


def _git_commit(repo: Path, message: str = "init") -> str:
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "commit", "-qm", message], cwd=str(repo), check=True
    )
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def test_build_full_empty_repo(tmp_path: Path) -> None:
    """An empty git repo builds a valid (but empty) index."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    # No files — but git ls-files returns empty, the build should still
    # succeed and create the schema.
    db = index_db_path(repo)
    stats = IndexBuilder.build_full(repo, db)
    assert stats.full_rebuild is True
    assert stats.file_count == 0
    assert stats.symbol_count == 0
    # Schema must be present.
    conn = sqlite3.connect(str(db))
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "files" in tables
    assert "symbols" in tables
    conn.close()


def test_build_full_small_fixture(tmp_path: Path) -> None:
    """3 py + 2 cpp files → indexed file count + symbol count > 0."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    (repo / "a.py").write_text("def fa():\n    return 1\n")
    (repo / "b.py").write_text(
        "class B:\n    def method(self):\n        pass\n"
    )
    (repo / "c.py").write_text("CONST = 7\n")
    (repo / "d.cpp").write_text(
        "namespace n { void freeFn() {} }\n"
    )
    (repo / "e.cpp").write_text(
        "class Foo {\npublic:\n  void bar();\n};\n"
    )
    _git_commit(repo)

    db = index_db_path(repo)
    stats = IndexBuilder.build_full(repo, db)
    assert stats.file_count == 5
    assert stats.symbol_count >= 5  # at least one per file


def test_schema_version_recorded(tmp_path: Path) -> None:
    """`meta.index_version` matches :data:`INDEX_SCHEMA_VERSION` after build."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    (repo / "a.py").write_text("def f(): pass\n")
    _git_commit(repo)
    db = index_db_path(repo)
    IndexBuilder.build_full(repo, db)
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT value FROM meta WHERE key='index_version'"
    ).fetchone()
    assert row is not None
    assert row[0] == INDEX_SCHEMA_VERSION
    conn.close()


def test_full_rebuild_on_version_mismatch(tmp_path: Path) -> None:
    """Stored schema version != current → build_incremental delegates to full."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    (repo / "a.py").write_text("def f(): pass\n")
    _git_commit(repo)
    db = index_db_path(repo)
    IndexBuilder.build_full(repo, db)
    # Corrupt the recorded schema version.
    conn = sqlite3.connect(str(db))
    conn.execute(
        "UPDATE meta SET value='0' WHERE key='index_version'"
    )
    conn.commit()
    conn.close()
    # Now incremental should delegate to full and bump back to "1".
    stats = IndexBuilder.build_incremental(
        repo, db, since_sha=None
    )
    assert stats.full_rebuild is True
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT value FROM meta WHERE key='index_version'"
    ).fetchone()
    assert row[0] == INDEX_SCHEMA_VERSION
    conn.close()


def test_incremental_picks_up_changed_files_via_git_diff(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    (repo / ".gitignore").write_text(".autodev/\n")
    (repo / "a.py").write_text("def fa(): pass\n")
    _git_commit(repo, "init")
    db = index_db_path(repo)
    IndexBuilder.build_full(repo, db)
    sha_before = _last_indexed_sha(db)
    assert sha_before is not None

    (repo / "b.py").write_text("def fb(): pass\n")
    _git_commit(repo, "add b")
    stats = IndexBuilder.build_incremental(
        repo, db, since_sha=sha_before
    )
    assert stats.full_rebuild is False
    conn = sqlite3.connect(str(db))
    paths = {
        row[0]
        for row in conn.execute("SELECT path FROM files").fetchall()
    }
    assert "a.py" in paths
    assert "b.py" in paths
    conn.close()


def test_incremental_falls_back_to_mtime_when_git_diff_fails(
    tmp_path: Path,
) -> None:
    """Bogus since_sha → git diff fails → mtime fallback re-scans everything."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    (repo / ".gitignore").write_text(".autodev/\n")
    (repo / "a.py").write_text("def fa(): pass\n")
    _git_commit(repo)
    db = index_db_path(repo)
    IndexBuilder.build_full(repo, db)

    (repo / "b.py").write_text("def fb(): pass\n")
    # Don't commit b.py — but the mtime fallback should still pick it up
    # because it walks all files (tracked + the new one is covered when
    # the git ls-files iterator is bypassed). Actually our walker uses
    # git ls-files so untracked files won't appear; commit it.
    _git_commit(repo, "add b")
    stats = IndexBuilder.build_incremental(
        repo, db, since_sha="0" * 40  # bogus sha → git diff fails
    )
    # Mtime path is reported as full_rebuild=False (no actual rebuild).
    assert stats.full_rebuild is False
    conn = sqlite3.connect(str(db))
    paths = {
        row[0]
        for row in conn.execute("SELECT path FROM files").fetchall()
    }
    assert "b.py" in paths
    conn.close()


def test_full_rebuild_threshold_triggered(tmp_path: Path) -> None:
    """Changed-set > threshold triggers a full rebuild."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    (repo / ".gitignore").write_text(".autodev/\n")
    for i in range(5):
        (repo / f"f{i}.py").write_text(f"def f{i}(): pass\n")
    _git_commit(repo, "init")
    db = index_db_path(repo)
    IndexBuilder.build_full(repo, db)
    sha_before = _last_indexed_sha(db)

    # Add many files in one commit, then incremental with threshold=1
    # should route to full build.
    for i in range(10):
        (repo / f"new{i}.py").write_text(f"def n{i}(): pass\n")
    _git_commit(repo, "many")
    stats = IndexBuilder.build_incremental(
        repo, db, since_sha=sha_before, full_rebuild_threshold=2
    )
    assert stats.full_rebuild is True


def test_lock_blocks_concurrent_build(tmp_path: Path) -> None:
    """Active-PID lock makes a second builder no-op (raises contention)."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    (repo / "a.py").write_text("def f(): pass\n")
    _git_commit(repo)
    db = index_db_path(repo)
    db.parent.mkdir(parents=True, exist_ok=True)
    # Pre-write a lock file with our own PID (alive).
    lock = _lock_path(db)
    lock.write_text(f"{os.getpid()} 2026-01-01T00:00:00+00:00\n", encoding="utf-8")
    with pytest.raises(IndexBuildContentionError):
        IndexBuilder.build_full(repo, db)


def test_state_file_atomic_rename(tmp_path: Path) -> None:
    """`.autodev/index.state.json` is written via .tmp + os.replace.

    We test the contract by mocking ``os.replace`` to confirm it gets
    invoked with a ``.tmp`` source path (i.e. atomic rename pattern is
    in use rather than direct write).
    """
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    (repo / ".gitignore").write_text(".autodev/\n")
    (repo / "a.py").write_text("def f(): pass\n")
    _git_commit(repo)
    db = index_db_path(repo)
    real_replace = os.replace
    seen: list[tuple[str, str]] = []

    def spy_replace(src, dst):  # type: ignore[no-untyped-def]
        seen.append((str(src), str(dst)))
        real_replace(src, dst)

    with patch("state.file_index.os.replace", spy_replace):
        IndexBuilder.build_full(repo, db)

    assert any(
        src.endswith(".tmp") and dst.endswith("index.state.json")
        for src, dst in seen
    )
    # And the actual state file must be valid JSON with the expected keys.
    sp = _state_path(db)
    payload = json.loads(sp.read_text())
    assert payload["schema_version"] == INDEX_SCHEMA_VERSION
    assert "last_indexed_at" in payload
    assert "last_indexed_sha" in payload
