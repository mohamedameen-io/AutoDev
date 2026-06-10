"""Parity + correctness tests for the parallelized index build.

Covers the two-step parallelization of :meth:`IndexBuilder.build_full`:

  * Step 1 / Step 2 parity: ``workers=1`` and ``workers=4`` must produce
    identical ``files`` + ``symbols`` rows (modulo autoincrement ``id``)
    and identical :class:`IndexQuery` results.
  * FTS correctness after the external-content ``('rebuild')`` path.
  * Edge cases: binary/unparseable file, empty repo, a file deleted
    before the worker parses it (worker returns ``None``).
  * Async-entry regression: ``python -m state.file_index build-full`` must
    produce a NON-EMPTY index (it was previously a silent no-op).

Fixture pattern mirrors ``tests/test_state_file_index_query.py``.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from state.file_index import IndexBuilder, IndexQuery


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=str(repo), check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=str(repo), check=True
    )


def _git_commit(repo: Path) -> None:
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "commit", "-qm", "init"], cwd=str(repo), check=True
    )


def _write_sources(repo: Path) -> None:
    """Write a small multi-language source tree under *repo*."""
    (repo / ".gitignore").write_text(".autodev/\n")
    (repo / "src").mkdir()
    (repo / "src" / "parse_plan.py").write_text(
        "def parse_plan_markdown(md: str) -> dict:\n"
        "    return {}\n"
        "\n"
        "class PlanParseError(Exception):\n"
        "    pass\n"
    )
    (repo / "src" / "validate_files.py").write_text(
        "def validate_files_exist(plan, cwd):\n"
        "    return None\n"
    )
    (repo / "src" / "common.py").write_text(
        "def helper():\n"
        "    return 1\n"
        "\n"
        "MAX_RETRIES = 3\n"
    )
    (repo / "src" / "widget.cpp").write_text(
        "namespace ns {\n"
        "int compute_widget(int a) { return a; }\n"
        "}\n"
    )
    (repo / "src" / "app.ts").write_text(
        "export function renderApp(props: any) { return props; }\n"
        "export class AppController {}\n"
    )
    (repo / "README.md").write_text("# fixture repo\n")


def _make_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git_init(repo)
    _write_sources(repo)
    _git_commit(repo)
    return repo


def _dump_files(db: Path) -> list[tuple]:
    """Return ``files`` rows ordered by path (id-independent)."""
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT path, content_hash, mtime_ns, size_bytes, lang "
            "FROM files ORDER BY path"
        ).fetchall()
    finally:
        conn.close()
    return rows


def _dump_symbols(db: Path) -> list[tuple]:
    """Return ``symbols`` rows ordered deterministically, sans ``id``."""
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT file_path, name, kind, signature, line, col "
            "FROM symbols ORDER BY file_path, name, kind, line, col"
        ).fetchall()
    finally:
        conn.close()
    return rows


# ---------------------------------------------------------------------------
# Parity: serial (workers=1) vs parallel (workers=4)
# ---------------------------------------------------------------------------


def test_serial_vs_parallel_files_and_symbols_identical(tmp_path: Path) -> None:
    """workers=1 and workers=4 produce identical files + symbols rows."""
    repo = _make_repo(tmp_path, "r")
    db_serial = tmp_path / "serial.db"
    db_par = tmp_path / "parallel.db"

    IndexBuilder.build_full(repo, db_serial, workers=1)
    IndexBuilder.build_full(repo, db_par, workers=4)

    assert _dump_files(db_serial) == _dump_files(db_par)
    assert _dump_symbols(db_serial) == _dump_symbols(db_par)
    # Sanity: the fixture actually produced rows.
    assert len(_dump_files(db_serial)) >= 6
    assert len(_dump_symbols(db_serial)) >= 3


def test_serial_vs_parallel_query_results_identical(tmp_path: Path) -> None:
    """IndexQuery results identical across workers=1 and workers=4."""
    repo = _make_repo(tmp_path, "r")
    db_serial = tmp_path / "serial.db"
    db_par = tmp_path / "parallel.db"

    IndexBuilder.build_full(repo, db_serial, workers=1)
    IndexBuilder.build_full(repo, db_par, workers=4)

    qs = IndexQuery(db_serial)
    qp = IndexQuery(db_par)
    try:
        for term in ("parse_plan_markdown", "parse", "validate", "widget", "render"):
            assert qs.search_symbols(term) == qp.search_symbols(term), term
        for pat in ("parse_plan", "src", "validate", ".py", "app"):
            assert qs.search_files(pat) == qp.search_files(pat), pat
        for spec in (
            "refactor parsePlanMarkdown and validateFilesExist",
            "render the app controller widget",
        ):
            ds = qs.get_candidates_for_spec(spec)
            dp = qp.get_candidates_for_spec(spec)
            assert ds.symbol_hits == dp.symbol_hits, spec
            assert ds.file_hits == dp.file_hits, spec
    finally:
        qs.close()
        qp.close()


def test_fts_hits_correct_after_rebuild(tmp_path: Path) -> None:
    """symbols_fts / files_fts MATCH return expected fixture hits.

    Exercises the external-content FTS ``('rebuild')`` path (Step 2) plus
    the standalone trigram ``files_fts``.
    """
    repo = _make_repo(tmp_path, "r")
    db = tmp_path / "idx.db"
    IndexBuilder.build_full(repo, db, workers=4)

    conn = sqlite3.connect(str(db))
    try:
        # External-content symbols_fts MATCH must resolve via rowid join.
        rows = conn.execute(
            "SELECT s.name FROM symbols s JOIN symbols_fts f ON s.id = f.rowid "
            "WHERE symbols_fts MATCH 'parse_plan_markdown'"
        ).fetchall()
        assert any(r[0] == "parse_plan_markdown" for r in rows)

        # files_fts trigram MATCH (standalone).
        frows = conn.execute(
            "SELECT path FROM files_fts WHERE files_fts MATCH 'parse_plan'"
        ).fetchall()
        assert any("parse_plan.py" in r[0] for r in frows)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_binary_unparseable_file_indexed_with_no_symbols(tmp_path: Path) -> None:
    """A binary file appears in ``files`` (lang=other) with no symbols."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    (repo / ".gitignore").write_text(".autodev/\n")
    (repo / "good.py").write_text("def f():\n    return 1\n")
    (repo / "blob.bin").write_bytes(bytes(range(256)) * 4)
    _git_commit(repo)

    db_serial = tmp_path / "serial.db"
    db_par = tmp_path / "parallel.db"
    IndexBuilder.build_full(repo, db_serial, workers=1)
    IndexBuilder.build_full(repo, db_par, workers=4)

    assert _dump_files(db_serial) == _dump_files(db_par)
    assert _dump_symbols(db_serial) == _dump_symbols(db_par)

    files = {r[0] for r in _dump_files(db_par)}
    assert "blob.bin" in files
    # No symbols attributed to the binary file.
    bin_syms = [s for s in _dump_symbols(db_par) if s[0] == "blob.bin"]
    assert bin_syms == []


def test_empty_repo(tmp_path: Path) -> None:
    """An empty repo builds a valid, empty index for both worker counts."""
    repo = tmp_path / "empty"
    repo.mkdir()
    _git_init(repo)
    # A single committed file we then ignore: keep repo non-degenerate for
    # git, but exercise the "no source files of interest" shape by only
    # committing a gitignore (git ls-files yields just .gitignore).
    (repo / ".gitignore").write_text(".autodev/\n")
    _git_commit(repo)

    db_serial = tmp_path / "serial.db"
    db_par = tmp_path / "parallel.db"
    stats_s = IndexBuilder.build_full(repo, db_serial, workers=1)
    stats_p = IndexBuilder.build_full(repo, db_par, workers=4)

    assert _dump_files(db_serial) == _dump_files(db_par)
    assert _dump_symbols(db_serial) == _dump_symbols(db_par)
    assert stats_s.symbol_count == stats_p.symbol_count == 0
    # IndexQuery opens cleanly and returns nothing.
    q = IndexQuery(db_par)
    try:
        assert q.search_symbols("anything") == []
    finally:
        q.close()


def test_truly_empty_repo_no_tracked_files(tmp_path: Path) -> None:
    """A git repo with zero tracked files builds an empty index."""
    repo = tmp_path / "void"
    repo.mkdir()
    _git_init(repo)

    db = tmp_path / "void.db"
    stats = IndexBuilder.build_full(repo, db, workers=4)
    assert stats.file_count == 0
    assert stats.symbol_count == 0
    assert _dump_files(db) == []


def test_file_deleted_before_parse_returns_none(tmp_path: Path) -> None:
    """A path that git tracks but vanishes before parse is skipped cleanly.

    Simulates the worker returning ``None`` for a removed file: the index
    build must not crash and must omit the missing file.
    """
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    (repo / ".gitignore").write_text(".autodev/\n")
    (repo / "keep.py").write_text("def keep():\n    return 1\n")
    (repo / "ghost.py").write_text("def ghost():\n    return 2\n")
    _git_commit(repo)

    # Delete ghost.py AFTER commit so git ls-files still lists it but the
    # worker cannot read it.
    (repo / "ghost.py").unlink()

    db = tmp_path / "idx.db"
    stats = IndexBuilder.build_full(repo, db, workers=4)

    files = {r[0] for r in _dump_files(db)}
    assert "keep.py" in files
    assert "ghost.py" not in files
    assert stats.file_count >= 1


def test_default_workers_zero_builds_index(tmp_path: Path) -> None:
    """workers=0 (auto = cpu_count) builds a valid, non-empty index."""
    repo = _make_repo(tmp_path, "r")
    db = tmp_path / "idx.db"
    stats = IndexBuilder.build_full(repo, db)  # default workers=0
    assert stats.file_count >= 6
    assert stats.symbol_count >= 3


def test_backward_compatible_signature(tmp_path: Path) -> None:
    """Existing callers using build_full(cwd, db) keep working unchanged."""
    repo = _make_repo(tmp_path, "r")
    db = tmp_path / "idx.db"
    stats = IndexBuilder.build_full(repo, db)
    assert stats.file_count >= 6


# ---------------------------------------------------------------------------
# Async-entry regression: the __main__ build-full entrypoint
# ---------------------------------------------------------------------------


def test_async_entry_module_main_produces_nonempty_index(tmp_path: Path) -> None:
    """`python -m state.file_index build-full` must build a NON-EMPTY index.

    Guards the bug where the module had no ``__main__`` block, so the
    async build subprocess loaded the module and exited doing nothing.
    """
    repo = _make_repo(tmp_path, "r")
    db = tmp_path / "async.db"

    src_dir = Path(__file__).resolve().parents[1] / "src"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(src_dir) + os.pathsep + env.get("PYTHONPATH", "")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "state.file_index",
            "build-full",
            "--cwd",
            str(repo),
            "--db",
            str(db),
            "--workers",
            "2",
        ],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert db.exists(), result.stderr
    files = _dump_files(db)
    symbols = _dump_symbols(db)
    assert len(files) >= 6, f"empty index: {result.stderr}"
    assert len(symbols) >= 3, f"no symbols: {result.stderr}"
