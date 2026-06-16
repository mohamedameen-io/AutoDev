"""Sqlite-FTS5 file/symbol index for planner candidate lookup (v0.25.0).

The index database lives at ``.autodev/index.db`` and is shared (WAL
mode) across all per-task worktrees under
``.autodev/execute_worktrees_pool/``. Worktrees access the index as
**read-only consumers** via :class:`IndexQuery`. Only the orchestrator's
main process — running in the repo root before any worktree spawn —
invokes :class:`IndexBuilder` ``build_*`` methods. WAL mode allows
arbitrary concurrent readers; the single-writer constraint is enforced
via ``.autodev/index.db.lock``.

Three on-disk safety guards back the builder:

1. **Builder lock file** — ``.autodev/index.db.lock`` records the holder
   PID + ISO8601 timestamp. Stale-lock detection mirrors
   ``state/lockfile.py`` (v0.23.0 C3): dead-PID locks are auto-cleared
   with a warning; alive-PID locks surface as a no-op skip.
2. **Build marker file** — ``.autodev/index.db.building`` is created
   *before* schema initialization and removed on success. Per-trigger
   hooks (``execute``, ``plan``, ``resume``) skip the incremental update
   when the marker is present (a full build is in progress, e.g. async
   on a huge repo).
3. **Atomic state file** — ``.autodev/index.state.json`` records
   ``{last_indexed_sha, last_indexed_at, schema_version,
   last_full_rebuild_at}``. Written via the ``.tmp`` + :func:`os.replace`
   atomic-rename pattern so partial writes can never corrupt operator
   forensics.

Killed cooperatively by either ``cfg.index_enabled = False`` (per
workspace) or the process-level env var ``AUTODEV_INDEX_DISABLED=1``.
The env var path raises :class:`IndexDisabledError` on the first call to
:meth:`IndexBuilder.build_full` / :meth:`IndexQuery.__init__`; call
sites catch and log the disabled state as a benign no-op.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import multiprocessing as mp
import os
import re
import sqlite3
import subprocess
import time
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from errors import AutodevError
from runtime.repo_probe import iter_repo_files
from state.language_extractors import lookup_extractor


_log = logging.getLogger(__name__)


INDEX_SCHEMA_VERSION: str = "1"


# ---------------------------------------------------------------------------
# Public dataclasses (renderable / queryable surfaces)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IndexStats:
    """Summary of an :class:`IndexBuilder` run.

    Attributes:
        file_count: Files indexed in total (after the run).
        symbol_count: Symbols indexed in total (after the run).
        duration_ms: Wall-clock duration of the build in milliseconds.
        full_rebuild: True if this run rebuilt from scratch (build_full
            or an incremental that exceeded the rebuild threshold).
    """

    file_count: int
    symbol_count: int
    duration_ms: int
    full_rebuild: bool


@dataclass(frozen=True)
class SymbolHit:
    """One symbol returned by an :class:`IndexQuery` search."""

    name: str
    kind: str
    file_path: str
    line: int
    signature: str


@dataclass(frozen=True)
class FileHit:
    """One file returned by an :class:`IndexQuery` search."""

    path: str
    lang: str


@dataclass(frozen=True)
class CandidateDigest:
    """Renderable digest for the architect's planning prompt.

    The :meth:`render` output is bounded by ``max_chars``; on overflow
    :attr:`truncated` is True and a ``"(truncated; N additional matches
    omitted)"`` line is appended. The injection point is
    :class:`orchestrator.delegation_envelope.DelegationEnvelope.context`
    (key ``"candidate_files"``).
    """

    symbol_hits: list[SymbolHit] = field(default_factory=list)
    file_hits: list[FileHit] = field(default_factory=list)
    truncated: bool = False

    def render(self, max_chars: int = 2500) -> str:
        """Render the digest as a plain-text block. Bounded by *max_chars*.

        Format::

            CANDIDATE_FILES (top matches from repo index — prefer these
            over invented paths):

            Symbols:
              - parse_plan_markdown (function) src/orchestrator/plan_parser.py:141 — `<sig>`
              ...

            Files:
              src/orchestrator/  — 14 files: plan_parser.py, plan_phase.py, ...
              src/state/         — 9 files: paths.py, schemas.py, ...

        On overflow we append ``(truncated; N additional matches
        omitted)``. ``N`` is the count of items dropped during render.
        """
        if not self.symbol_hits and not self.file_hits:
            return ""

        lines: list[str] = [
            "CANDIDATE_FILES (top matches from repo index — "
            "prefer these over invented paths):",
            "",
        ]

        dropped = 0

        if self.symbol_hits:
            lines.append("Symbols:")
            for hit in self.symbol_hits:
                line = (
                    f"  - {hit.name} ({hit.kind}) "
                    f"{hit.file_path}:{hit.line} — `{hit.signature}`"
                )
                projected = sum(len(s) + 1 for s in lines) + len(line) + 1
                if projected > max_chars:
                    dropped += 1
                    continue
                lines.append(line)
            lines.append("")

        # Group files by parent dir for the digest's "Files:" section.
        if self.file_hits:
            by_parent: dict[str, list[FileHit]] = {}
            for f in self.file_hits:
                parent = str(Path(f.path).parent).replace(os.sep, "/")
                if parent in (".", ""):
                    parent = "."
                by_parent.setdefault(parent, []).append(f)

            lines.append("Files:")
            for parent, fhits in sorted(by_parent.items()):
                names = ", ".join(Path(f.path).name for f in fhits[:8])
                if len(fhits) > 8:
                    names += f", … (+{len(fhits) - 8})"
                line = f"  {parent}/  — {len(fhits)} files: {names}"
                projected = sum(len(s) + 1 for s in lines) + len(line) + 1
                if projected > max_chars:
                    dropped += len(fhits)
                    continue
                lines.append(line)

        out = "\n".join(lines)
        truncated = self.truncated or dropped > 0 or len(out) > max_chars
        if truncated and dropped > 0:
            out = (
                out
                + f"\n(truncated; {dropped} additional matches omitted)"
            )
        elif truncated:
            out = out + "\n(truncated)"

        if len(out) > max_chars:
            # Hard cap as last resort; preserves the truncation marker.
            head = out[: max_chars - 80].rstrip()
            out = head + "\n…\n(truncated)"
        return out


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class IndexDisabledError(AutodevError):
    """Raised when the env var ``AUTODEV_INDEX_DISABLED=1`` is set.

    Both :meth:`IndexBuilder.build_full` /
    :meth:`IndexBuilder.build_incremental` and :meth:`IndexQuery.__init__`
    check the env var and raise this exception. Call sites are expected
    to ``except IndexDisabledError: pass`` (with a debug log).
    """


class IndexBuildContentionError(AutodevError):
    """Raised when an :class:`IndexBuilder` run was a no-op due to a held lock.

    Operators receive this in ``autodev doctor`` output so they can
    confirm a parallel build is in flight rather than a silent drop.
    """


# ---------------------------------------------------------------------------
# Sqlite schema + connection helpers
# ---------------------------------------------------------------------------


_SCHEMA_DDL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS files (
    path          TEXT PRIMARY KEY,
    content_hash  TEXT NOT NULL,
    mtime_ns      INTEGER NOT NULL,
    size_bytes    INTEGER NOT NULL,
    lang          TEXT NOT NULL,
    indexed_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_lang ON files(lang);

CREATE TABLE IF NOT EXISTS symbols (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path     TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL,
    signature     TEXT NOT NULL DEFAULT '',
    line          INTEGER NOT NULL,
    col           INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_path);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);

CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(
    name, file_path, signature,
    content='symbols', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS symbols_ai AFTER INSERT ON symbols BEGIN
    INSERT INTO symbols_fts(rowid, name, file_path, signature)
    VALUES (new.id, new.name, new.file_path, new.signature);
END;
CREATE TRIGGER IF NOT EXISTS symbols_ad AFTER DELETE ON symbols BEGIN
    INSERT INTO symbols_fts(symbols_fts, rowid, name, file_path, signature)
    VALUES('delete', old.id, old.name, old.file_path, old.signature);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
    path,
    tokenize='trigram case_sensitive 0'
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _open_writer(db_path: Path) -> sqlite3.Connection:
    """Open a sqlite connection for writes, ensuring WAL mode and FK on."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0, isolation_level=None)
    conn.executescript(_SCHEMA_DDL)
    return conn


def _open_reader(db_path: Path) -> sqlite3.Connection:
    """Open a sqlite connection for read-only queries."""
    if not db_path.exists():
        raise FileNotFoundError(f"index db not found: {db_path}")
    # uri=True lets us request immutable=1 / mode=ro from the URI
    # qualifier. We ask for read-only with a small timeout.
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=15.0)
    return conn


# ---------------------------------------------------------------------------
# Lock + marker + state-file helpers
# ---------------------------------------------------------------------------


def _lock_path(db_path: Path) -> Path:
    return db_path.parent / "index.db.lock"


def _building_marker_path(db_path: Path) -> Path:
    return db_path.parent / "index.db.building"


def _state_path(db_path: Path) -> Path:
    return db_path.parent / "index.state.json"


def _read_lock_holder(p: Path) -> tuple[int | None, str | None]:
    try:
        text = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None, None
    if not text:
        return None, None
    parts = text.split(maxsplit=1)
    try:
        pid = int(parts[0])
    except (ValueError, IndexError):
        return None, None
    ts = parts[1] if len(parts) > 1 else None
    return pid, ts


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@contextmanager
def _builder_lock(db_path: Path) -> Iterator[bool]:
    """Try to acquire the builder lock. Yields True on success.

    Mirrors the v0.23.0 C3 :mod:`state.lockfile` pattern: dead-PID locks
    auto-clear with a warning; alive-PID locks yield ``False`` so the
    caller can no-op gracefully.
    """
    lock = _lock_path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if lock.exists():
        prev_pid, prev_ts = _read_lock_holder(lock)
        if prev_pid is not None:
            if _is_pid_alive(prev_pid):
                _log.warning(
                    "file_index.lock_held_by_active_process pid=%s started_at=%s",
                    prev_pid,
                    prev_ts,
                )
                yield False
                return
            else:
                _log.warning(
                    "file_index.stale_lock_cleared pid=%s started_at=%s",
                    prev_pid,
                    prev_ts,
                )
                # Continue — overwrite below.

    try:
        ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
        lock.write_text(f"{os.getpid()} {ts}\n", encoding="utf-8")
    except OSError as exc:
        _log.warning("file_index.lock_write_failed err=%s", str(exc))
        yield False
        return

    try:
        yield True
    finally:
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass


def _write_state_atomic(db_path: Path, payload: dict[str, str | int]) -> None:
    """Atomic write of ``.autodev/index.state.json`` (``.tmp`` + replace)."""
    sp = _state_path(db_path)
    sp.parent.mkdir(parents=True, exist_ok=True)
    tmp = sp.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, sp)


def _read_state(db_path: Path) -> dict[str, str | int]:
    sp = _state_path(db_path)
    if not sp.exists():
        return {}
    try:
        return json.loads(sp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _last_indexed_sha(db_path: Path) -> str | None:
    """Public-ish helper for the per-trigger hook."""
    state = _read_state(db_path)
    val = state.get("last_indexed_sha")
    return str(val) if val else None


def _check_disabled() -> None:
    """Raise :class:`IndexDisabledError` when the env var kill switch is set."""
    if os.environ.get("AUTODEV_INDEX_DISABLED", "").strip() == "1":
        raise IndexDisabledError(
            "file index disabled via AUTODEV_INDEX_DISABLED=1"
        )


# ---------------------------------------------------------------------------
# Symbol extraction + content hashing
# ---------------------------------------------------------------------------


def _content_hash(text: bytes) -> str:
    """Return the first 16 hex chars of sha256(text)."""
    return hashlib.sha256(text).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Parallel parse (CPU-bound, GIL-bound → processes, not threads)
# ---------------------------------------------------------------------------
#
# The parse stage (read bytes → hash → stat → decode → extractor.extract())
# is CPU- and GIL-bound, so we fan it out across worker *processes*. The
# write stage stays single-threaded in the parent (sqlite single-writer).
# Serial and parallel paths feed the SAME writer (``_bulk_write`` /
# ``_index_parsed_serial``) → parity by construction.
#
# macOS defaults to the ``spawn`` start method, so the worker function and
# its arguments must be top-level + picklable. We reuse the ``forkserver``
# context (mirrors ``qa/sandbox.py``). Workers re-import this module; the
# extractors are module-level singletons that re-init idempotently, so the
# worker just calls ``lookup_extractor(suffix)``. Workers do NO sqlite I/O.


@dataclass(frozen=True)
class _ParsedFile:
    """Result of parsing one file in a worker process (picklable).

    ``symbols`` is a tuple of ``(name, kind, signature, line, col)`` tuples
    so the dataclass is trivially picklable. ``rel`` is the repo-relative
    POSIX path used as the ``files.path`` primary key.
    """

    rel: str
    content_hash: str
    mtime_ns: int
    size_bytes: int
    lang: str
    symbols: tuple[tuple[str, str, str, int, int], ...]


# Set by the pool initializer in each worker (picklable string arg). Workers
# need the repo root to compute the repo-relative path.
_WORKER_CWD: str | None = None


def _init_worker(cwd: str) -> None:
    """Pool initializer: stash the repo root in a module global."""
    global _WORKER_CWD
    _WORKER_CWD = cwd


def _parse_one(abs_path: Path, cwd: Path) -> _ParsedFile | None:
    """Parse a single file into a :class:`_ParsedFile` (no DB access).

    Best-effort parity with :func:`_index_one_file`: read/decode/extract
    failures yield an empty symbol list; a path outside *cwd* or an
    unreadable/vanished file yields ``None`` (skipped).
    """
    try:
        rel = abs_path.relative_to(cwd).as_posix()
    except ValueError:
        return None
    try:
        raw = abs_path.read_bytes()
    except OSError:
        return None
    try:
        st = abs_path.stat()
    except OSError:
        return None
    chash = _content_hash(raw)
    extractor = lookup_extractor(abs_path.suffix.lower())
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    try:
        extracted = extractor.extract(text)
    except Exception as exc:  # noqa: BLE001 — never let one file kill the build
        _log.debug("file_index.extract_failed path=%s err=%s", rel, str(exc))
        extracted = []
    symbols = tuple(
        (
            sym["name"],
            sym["kind"],
            sym["signature"],
            int(sym["line"]),
            int(sym["col"]),
        )
        for sym in extracted
    )
    return _ParsedFile(
        rel=rel,
        content_hash=chash,
        mtime_ns=st.st_mtime_ns,
        size_bytes=st.st_size,
        lang=extractor.lang_tag,
        symbols=symbols,
    )


def _parse_file_worker(abs_str: str) -> _ParsedFile | None:
    """Top-level, picklable worker entrypoint for the process pool.

    Uses the pool-initialized ``_WORKER_CWD`` to resolve repo-relative
    paths. Returns ``None`` for files that should be skipped.
    """
    if _WORKER_CWD is None:  # pragma: no cover — initializer always runs first
        return None
    return _parse_one(Path(abs_str), Path(_WORKER_CWD))


def _iter_parsed(
    files: list[Path],
    cwd: Path,
    workers: int,
) -> Iterator[_ParsedFile]:
    """Yield :class:`_ParsedFile` for each file, parallel when ``workers>1``.

    Parallel path: a ``forkserver`` ``Pool`` runs :func:`_parse_file_worker`
    via ``imap_unordered``. On any pool-construction failure we fall back to
    the serial path so a build never hard-fails on a multiprocessing quirk.
    Serial path (``workers<=1`` or fallback): parse in-process. ``None``
    results (skipped files) are filtered out in both paths.
    """
    if workers > 1 and files:
        try:
            ctx = mp.get_context("forkserver")
            with ctx.Pool(
                processes=workers,
                initializer=_init_worker,
                initargs=(str(cwd),),
            ) as pool:
                for parsed in pool.imap_unordered(
                    _parse_file_worker,
                    [str(p) for p in files],
                    chunksize=64,
                ):
                    if parsed is not None:
                        yield parsed
            return
        except Exception as exc:  # noqa: BLE001 — fall back to serial
            _log.warning(
                "file_index.pool_init_failed workers=%d err=%s; "
                "falling back to serial parse",
                workers,
                str(exc),
            )

    # Serial fallback (workers<=1, empty file list, or pool failure).
    for abs_path in files:
        parsed = _parse_one(abs_path, cwd)
        if parsed is not None:
            yield parsed


def _index_one_file(
    conn: sqlite3.Connection,
    cwd: Path,
    abs_path: Path,
    indexed_at: int,
) -> tuple[bool, int]:
    """Index one file. Returns ``(was_changed, symbol_count)``.

    Best-effort: read errors and unparseable files are recorded with an
    empty symbol list so the file still appears in :class:`IndexQuery`
    file searches.
    """
    try:
        rel = abs_path.relative_to(cwd).as_posix()
    except ValueError:
        return False, 0
    try:
        raw = abs_path.read_bytes()
    except OSError:
        return False, 0
    chash = _content_hash(raw)
    try:
        st = abs_path.stat()
    except OSError:
        return False, 0
    mtime_ns = st.st_mtime_ns
    size = st.st_size
    extractor = lookup_extractor(abs_path.suffix.lower())

    # Skip when the existing row matches exactly (incremental fast path).
    cur = conn.execute(
        "SELECT content_hash, mtime_ns FROM files WHERE path=?", (rel,)
    )
    row = cur.fetchone()
    if row is not None and row[0] == chash and row[1] == mtime_ns:
        return False, 0

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")

    try:
        symbols = extractor.extract(text)
    except Exception as exc:  # noqa: BLE001 — never let one file kill the build
        _log.debug(
            "file_index.extract_failed path=%s err=%s", rel, str(exc)
        )
        symbols = []

    # Replace the file row + cascade-delete its symbols.
    conn.execute("DELETE FROM symbols WHERE file_path=?", (rel,))
    conn.execute("DELETE FROM files WHERE path=?", (rel,))
    conn.execute("DELETE FROM files_fts WHERE path=?", (rel,))
    conn.execute(
        "INSERT INTO files(path, content_hash, mtime_ns, size_bytes, lang, indexed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (rel, chash, mtime_ns, size, extractor.lang_tag, indexed_at),
    )
    conn.execute("INSERT INTO files_fts(path) VALUES (?)", (rel,))
    for sym in symbols:
        conn.execute(
            "INSERT INTO symbols(file_path, name, kind, signature, line, col) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                rel,
                sym["name"],
                sym["kind"],
                sym["signature"],
                int(sym["line"]),
                int(sym["col"]),
            ),
        )
    return True, len(symbols)


def _delete_file_row(conn: sqlite3.Connection, rel: str) -> None:
    """Remove a file + its symbols (used when the file vanished)."""
    conn.execute("DELETE FROM symbols WHERE file_path=?", (rel,))
    conn.execute("DELETE FROM files WHERE path=?", (rel,))
    conn.execute("DELETE FROM files_fts WHERE path=?", (rel,))


# ---------------------------------------------------------------------------
# Bulk-loading writer (Step 2) — single-threaded, batched, FTS rebuild
# ---------------------------------------------------------------------------


def _drop_symbols_fts_triggers(conn: sqlite3.Connection) -> None:
    """Drop the per-row external-content FTS sync triggers.

    During a bulk load we populate ``symbols_fts`` once via the
    ``('rebuild')`` command instead of firing ``symbols_ai`` on every
    INSERT (orders of magnitude faster on huge repos).
    """
    conn.execute("DROP TRIGGER IF EXISTS symbols_ai")
    conn.execute("DROP TRIGGER IF EXISTS symbols_ad")


# The AI/AD trigger bodies, factored out so the bulk path can re-create the
# exact same triggers it dropped (keeping ``build_incremental`` per-file
# inserts populating the external-content FTS).
_SYMBOLS_FTS_TRIGGERS_DDL = """
CREATE TRIGGER IF NOT EXISTS symbols_ai AFTER INSERT ON symbols BEGIN
    INSERT INTO symbols_fts(rowid, name, file_path, signature)
    VALUES (new.id, new.name, new.file_path, new.signature);
END;
CREATE TRIGGER IF NOT EXISTS symbols_ad AFTER DELETE ON symbols BEGIN
    INSERT INTO symbols_fts(symbols_fts, rowid, name, file_path, signature)
    VALUES('delete', old.id, old.name, old.file_path, old.signature);
END;
"""


def _recreate_symbols_fts_triggers(conn: sqlite3.Connection) -> None:
    """Re-create the ``symbols_ai`` / ``symbols_ad`` sync triggers."""
    conn.executescript(_SYMBOLS_FTS_TRIGGERS_DDL)


def _bulk_write(
    conn: sqlite3.Connection,
    parsed_iter: Iterable[_ParsedFile],
    indexed_at: int,
    batch_size: int,
    progress_cb: Callable[[int, int], None] | None,
    total: int,
    start: float,
) -> tuple[int, int]:
    """Bulk-load parsed files into a freshly-wiped index.

    Assumes the db was just created (``build_full`` wipes first), so there
    are no pre-existing rows to skip/delete. The ``symbols_fts``
    external-content sync triggers are dropped for the duration; FTS is
    repopulated once at the end via ``INSERT INTO
    symbols_fts(symbols_fts) VALUES('rebuild')``, then the triggers are
    re-created so ``build_incremental`` per-file inserts keep working.

    Commits every *batch_size* files to bound the WAL (no 600 MB
    single-transaction blob). Returns ``(file_count, symbol_count)``.

    Note: ``files_fts`` is a *standalone* trigram FTS (no triggers), so we
    insert its ``path`` rows directly alongside the ``files`` rows.
    """
    _drop_symbols_fts_triggers(conn)

    file_count = 0
    symbol_count = 0
    files_batch: list[tuple] = []
    files_fts_batch: list[tuple] = []
    symbols_batch: list[tuple] = []

    def _flush() -> None:
        if files_batch:
            conn.executemany(
                "INSERT INTO files(path, content_hash, mtime_ns, size_bytes, "
                "lang, indexed_at) VALUES (?, ?, ?, ?, ?, ?)",
                files_batch,
            )
            conn.executemany(
                "INSERT INTO files_fts(path) VALUES (?)", files_fts_batch
            )
        if symbols_batch:
            conn.executemany(
                "INSERT INTO symbols(file_path, name, kind, signature, line, "
                "col) VALUES (?, ?, ?, ?, ?, ?)",
                symbols_batch,
            )
        files_batch.clear()
        files_fts_batch.clear()
        symbols_batch.clear()

    conn.execute("BEGIN")
    in_txn = True
    try:
        for parsed in parsed_iter:
            files_batch.append(
                (
                    parsed.rel,
                    parsed.content_hash,
                    parsed.mtime_ns,
                    parsed.size_bytes,
                    parsed.lang,
                    indexed_at,
                )
            )
            files_fts_batch.append((parsed.rel,))
            for sym in parsed.symbols:
                symbols_batch.append(
                    (parsed.rel, sym[0], sym[1], sym[2], sym[3], sym[4])
                )
            file_count += 1
            symbol_count += len(parsed.symbols)

            if progress_cb is not None and (
                file_count % 1000 == 0 or file_count == total
            ):
                try:
                    progress_cb(file_count, total)
                except Exception:  # noqa: BLE001
                    pass
            if file_count % 5000 == 0:
                _log.info(
                    "file_index.build_progress files_done=%d files_total=%d "
                    "elapsed_ms=%d",
                    file_count,
                    total,
                    int((time.monotonic() - start) * 1000),
                )

            # Commit per batch to bound the WAL.
            if len(files_batch) >= batch_size:
                _flush()
                conn.execute("COMMIT")
                conn.execute("BEGIN")
        # Final partial batch.
        _flush()
        conn.execute("COMMIT")
        in_txn = False

        # One-shot external-content FTS rebuild → identical to the
        # trigger-populated index.
        conn.execute("INSERT INTO symbols_fts(symbols_fts) VALUES('rebuild')")
        # Restore the per-row sync triggers for build_incremental.
        _recreate_symbols_fts_triggers(conn)
    except Exception:
        if in_txn:
            conn.execute("ROLLBACK")
        # Best-effort: leave the schema with triggers re-created so a
        # subsequent incremental isn't left without FTS sync.
        try:
            _recreate_symbols_fts_triggers(conn)
        except sqlite3.DatabaseError:
            pass
        raise

    return file_count, symbol_count


def _set_meta(conn: sqlite3.Connection, **kv: str | int) -> None:
    for k, v in kv.items():
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (k, str(v)),
        )


def _get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    cur = conn.execute("SELECT value FROM meta WHERE key=?", (key,))
    row = cur.fetchone()
    return row[0] if row else None


def _current_git_sha(cwd: Path) -> str | None:
    if not (cwd / ".git").exists():
        return None
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if out.returncode == 0:
            return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def _git_diff_changed(cwd: Path, since_sha: str) -> list[str] | None:
    """Return the list of changed paths (added/modified/deleted) since *since_sha*.

    Returns ``None`` on any error (caller falls back to mtime).
    """
    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", f"{since_sha}..HEAD"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if out.returncode != 0:
            return None
        return [line.strip() for line in out.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError):
        return None


# ---------------------------------------------------------------------------
# IndexBuilder
# ---------------------------------------------------------------------------


# v0.25.0 default — see cfg.index_full_rebuild_threshold_files.
_DEFAULT_FULL_REBUILD_THRESHOLD = 5000


class IndexBuilder:
    """Build / refresh the file/symbol index.

    Methods are static because the builder holds no per-instance state —
    all state lives on disk (the sqlite db + the lock + marker + state
    files). Concurrent build calls coordinate via the on-disk lock.
    """

    @staticmethod
    def build_full(
        cwd: Path,
        db_path: Path,
        languages: list[str] | None = None,
        progress_cb: Callable[[int, int], None] | None = None,
        workers: int = 0,
        batch_size: int = 1000,
    ) -> IndexStats:
        """Rebuild the index from scratch over every tracked file.

        The parse stage (read + decode + symbol extraction) is fanned out
        across worker *processes* and feeds a single bulk-loading sqlite
        writer in the parent. Serial (``workers<=1``) and parallel paths
        share the same writer, so the produced index is identical.

        Args:
            cwd: Repo root.
            db_path: Where to write the sqlite db.
            languages: Optional allowlist of ``lang_tag`` values
                (``"py"``, ``"cpp"``, ``"ts"``, ``"other"``). When None
                (default), every language is indexed.
            progress_cb: Optional ``(files_done, files_total)`` callback.
                Called once per ~1000 files to give the operator
                liveness on huge repos.
            workers: Number of parse worker processes. ``0`` (default)
                means ``os.cpu_count() or 1``. ``1`` forces the serial
                in-process parse path.
            batch_size: Number of files per write transaction. Bounds the
                WAL so a huge repo doesn't accumulate one giant commit.

        Returns:
            :class:`IndexStats` summarizing the run.

        Raises:
            IndexDisabledError: when ``AUTODEV_INDEX_DISABLED=1``.
            IndexBuildContentionError: when another builder holds the
                lock (current build was a no-op).
        """
        _check_disabled()
        start = time.monotonic()

        with _builder_lock(db_path) as held:
            if not held:
                raise IndexBuildContentionError(
                    f"another builder holds {_lock_path(db_path)}"
                )

            marker = _building_marker_path(db_path)
            try:
                marker.write_text(
                    _dt.datetime.now(_dt.timezone.utc).isoformat()
                    + "\n",
                    encoding="utf-8",
                )
            except OSError as exc:
                _log.warning(
                    "file_index.marker_write_failed err=%s", str(exc)
                )

            # Wipe any prior db so we start clean. Use os.remove (not
            # connection drop) so WAL files are also discarded.
            for ext in ("", "-wal", "-shm", "-journal"):
                target = Path(str(db_path) + ext)
                if target.exists():
                    try:
                        target.unlink()
                    except OSError as exc:
                        _log.warning(
                            "file_index.wipe_failed path=%s err=%s",
                            str(target),
                            str(exc),
                        )

            conn = _open_writer(db_path)
            try:
                _set_meta(
                    conn,
                    index_version=INDEX_SCHEMA_VERSION,
                )

                indexed_at = int(time.time())
                resolved_workers = workers if workers > 0 else (
                    os.cpu_count() or 1
                )
                files = list(iter_repo_files(cwd))
                total = len(files)

                # Parse stage (parallel via forkserver, else serial). The
                # ``files_fts`` rows + symbols are written serially in the
                # parent by ``_bulk_write``, which shares the parse output
                # with the serial path → parity by construction.
                parsed_iter = _iter_parsed(files, cwd, resolved_workers)
                if languages is not None:
                    allow = set(languages)
                    parsed_iter = (
                        p for p in parsed_iter if p.lang in allow
                    )

                file_count, symbol_count = _bulk_write(
                    conn,
                    parsed_iter,
                    indexed_at,
                    batch_size,
                    progress_cb,
                    total,
                    start,
                )

                sha = _current_git_sha(cwd)
                duration_ms = int((time.monotonic() - start) * 1000)
                final_file_count = conn.execute(
                    "SELECT COUNT(*) FROM files"
                ).fetchone()[0]
                final_symbol_count = conn.execute(
                    "SELECT COUNT(*) FROM symbols"
                ).fetchone()[0]
                _set_meta(
                    conn,
                    last_indexed_sha=sha or "",
                    last_indexed_at=indexed_at,
                    file_count=final_file_count,
                    symbol_count=final_symbol_count,
                    build_duration_ms=duration_ms,
                )
            finally:
                conn.close()

            try:
                marker.unlink(missing_ok=True)
            except OSError:
                pass

            duration_ms = int((time.monotonic() - start) * 1000)
            sha = _current_git_sha(cwd)
            _write_state_atomic(
                db_path,
                {
                    "last_indexed_sha": sha or "",
                    "last_indexed_at": int(time.time()),
                    "schema_version": INDEX_SCHEMA_VERSION,
                    "last_full_rebuild_at": int(time.time()),
                },
            )
            return IndexStats(
                file_count=final_file_count,
                symbol_count=final_symbol_count,
                duration_ms=duration_ms,
                full_rebuild=True,
            )

    @staticmethod
    def build_incremental(
        cwd: Path,
        db_path: Path,
        since_sha: str | None,
        full_rebuild_threshold: int = _DEFAULT_FULL_REBUILD_THRESHOLD,
        workers: int = 0,
        batch_size: int = 1000,
    ) -> IndexStats:
        """Incrementally update the index.

        If the db doesn't exist yet OR its schema version doesn't match
        :data:`INDEX_SCHEMA_VERSION`, delegates to :meth:`build_full`.

        If *since_sha* is given and ``git diff`` succeeds, only the
        changed paths are re-indexed (deletions removed, others
        re-extracted). When the changed-set exceeds *full_rebuild_threshold*,
        delegates to :meth:`build_full`.

        If *since_sha* is None or ``git diff`` fails, falls back to an
        mtime-vs-row-mtime check across the full file inventory (still
        cheap because :func:`_index_one_file` skips unchanged rows).

        ``workers`` / ``batch_size`` are forwarded only to the
        :meth:`build_full` delegations (the targeted-changed path stays
        serial — change-sets are small).

        Idempotent: holds ``.autodev/index.db.lock`` for the duration;
        on lock-held returns a no-op :class:`IndexStats`.
        """
        _check_disabled()
        start = time.monotonic()

        if not db_path.exists():
            return IndexBuilder.build_full(
                cwd, db_path, workers=workers, batch_size=batch_size
            )

        # Schema-version check (cheap migration trigger).
        try:
            r = _open_reader(db_path)
            try:
                row = r.execute(
                    "SELECT value FROM meta WHERE key='index_version'"
                ).fetchone()
                stored_version = row[0] if row else None
            finally:
                r.close()
        except sqlite3.DatabaseError:
            stored_version = None
        if stored_version != INDEX_SCHEMA_VERSION:
            _log.info(
                "file_index.schema_version_mismatch stored=%s expected=%s",
                stored_version,
                INDEX_SCHEMA_VERSION,
            )
            return IndexBuilder.build_full(
                cwd, db_path, workers=workers, batch_size=batch_size
            )

        # If git diff is available + since_sha is set, peek at the
        # changed-set BEFORE acquiring the lock so we can route to
        # build_full (which re-acquires) without lock-acquire overlap.
        peeked_changed: list[str] | None = None
        if since_sha:
            peeked_changed = _git_diff_changed(cwd, since_sha)
            if (
                peeked_changed is not None
                and len(peeked_changed) > full_rebuild_threshold
            ):
                _log.info(
                    "file_index.threshold_triggered changed=%d threshold=%d",
                    len(peeked_changed),
                    full_rebuild_threshold,
                )
                return IndexBuilder.build_full(
                    cwd, db_path, workers=workers, batch_size=batch_size
                )

        with _builder_lock(db_path) as held:
            if not held:
                # No-op skip: report current counts.
                return _read_only_stats(db_path, start)

            # Skip if a full build is currently running (marker present).
            if _building_marker_path(db_path).exists():
                return _read_only_stats(db_path, start)

            changed = peeked_changed
            if changed is None:
                # Either no since_sha OR git diff failed. Fall back to
                # the mtime-based rescan (covered by
                # :func:`_full_rebuild_inplace_held`, which assumes the
                # lock is already held — we're inside the with-block).
                return _full_rebuild_inplace_held(cwd, db_path, start)

            # Targeted incremental: process only the changed paths.
            conn = _open_writer(db_path)
            try:
                conn.execute("BEGIN")
                try:
                    indexed_at = int(time.time())
                    for rel in changed:
                        abs_path = cwd / rel
                        if not abs_path.exists():
                            _delete_file_row(conn, rel)
                            continue
                        _index_one_file(conn, cwd, abs_path, indexed_at)
                    sha = _current_git_sha(cwd)
                    final_file_count = conn.execute(
                        "SELECT COUNT(*) FROM files"
                    ).fetchone()[0]
                    final_symbol_count = conn.execute(
                        "SELECT COUNT(*) FROM symbols"
                    ).fetchone()[0]
                    duration_ms = int((time.monotonic() - start) * 1000)
                    _set_meta(
                        conn,
                        last_indexed_sha=sha or "",
                        last_indexed_at=indexed_at,
                        file_count=final_file_count,
                        symbol_count=final_symbol_count,
                        build_duration_ms=duration_ms,
                    )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            finally:
                conn.close()

            sha = _current_git_sha(cwd)
            _write_state_atomic(
                db_path,
                {
                    "last_indexed_sha": sha or "",
                    "last_indexed_at": int(time.time()),
                    "schema_version": INDEX_SCHEMA_VERSION,
                    "last_full_rebuild_at": int(
                        _read_state(db_path).get(
                            "last_full_rebuild_at", 0
                        )
                        or 0
                    ),
                },
            )
            return IndexStats(
                file_count=final_file_count,
                symbol_count=final_symbol_count,
                duration_ms=int((time.monotonic() - start) * 1000),
                full_rebuild=False,
            )


def _read_only_stats(db_path: Path, start: float) -> IndexStats:
    """Cheap helper: open the db read-only, return current counts as IndexStats.

    Used by the no-op skip path of :meth:`IndexBuilder.build_incremental`
    when another writer holds the lock OR the build marker is present.
    """
    conn_ro = _open_reader(db_path)
    try:
        fc = conn_ro.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        sc = conn_ro.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    finally:
        conn_ro.close()
    return IndexStats(
        file_count=fc,
        symbol_count=sc,
        duration_ms=int((time.monotonic() - start) * 1000),
        full_rebuild=False,
    )


def _full_rebuild_inplace_held(
    cwd: Path, db_path: Path, start: float
) -> IndexStats:
    """mtime-fallback incremental: scan every file, let row-skip short-circuit.

    Caller must already hold ``.autodev/index.db.lock``. Used by
    :meth:`IndexBuilder.build_incremental` when ``since_sha`` is None or
    git-diff failed.
    """
    conn = _open_writer(db_path)
    try:
        # Walk current files, mark seen rels.
        indexed_at = int(time.time())
        seen: set[str] = set()
        conn.execute("BEGIN")
        try:
            for abs_path in iter_repo_files(cwd):
                try:
                    rel = abs_path.relative_to(cwd).as_posix()
                except ValueError:
                    continue
                seen.add(rel)
                _index_one_file(conn, cwd, abs_path, indexed_at)

            # Drop rows for files that vanished.
            cur = conn.execute("SELECT path FROM files")
            existing = {row[0] for row in cur.fetchall()}
            for vanished in existing - seen:
                _delete_file_row(conn, vanished)

            sha = _current_git_sha(cwd)
            final_file_count = conn.execute(
                "SELECT COUNT(*) FROM files"
            ).fetchone()[0]
            final_symbol_count = conn.execute(
                "SELECT COUNT(*) FROM symbols"
            ).fetchone()[0]
            duration_ms = int((time.monotonic() - start) * 1000)
            _set_meta(
                conn,
                last_indexed_sha=sha or "",
                last_indexed_at=indexed_at,
                file_count=final_file_count,
                symbol_count=final_symbol_count,
                build_duration_ms=duration_ms,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    _write_state_atomic(
        db_path,
        {
            "last_indexed_sha": sha or "",
            "last_indexed_at": int(time.time()),
            "schema_version": INDEX_SCHEMA_VERSION,
            "last_full_rebuild_at": int(
                _read_state(db_path).get("last_full_rebuild_at", 0)
                or 0
            ),
        },
    )
    return IndexStats(
        file_count=final_file_count,
        symbol_count=final_symbol_count,
        duration_ms=int((time.monotonic() - start) * 1000),
        full_rebuild=False,
    )


# ---------------------------------------------------------------------------
# IndexQuery
# ---------------------------------------------------------------------------


# Identifier extraction: split snake_case + camelCase / PascalCase into
# unique tokens so spec text "refactor parsePlanMarkdown handler" hits
# both ``parsePlanMarkdown`` (FTS keyword) and ``parse``, ``plan``,
# ``markdown`` (per-token FTS).
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]+")
_CAMEL_SPLIT_RE = re.compile(
    r"(?<!^)(?=[A-Z][a-z])|(?<=[a-z0-9])(?=[A-Z])"
)


def _tokenize_spec(text: str) -> list[str]:
    """Return identifier tokens extracted from *text*.

    Splits camelCase / PascalCase and snake_case into individual words,
    preserves the original camel-cased form, lowercases everything for
    FTS tokenizer consistency. Drops tokens shorter than 3 chars
    (noisy: ``a``, ``id``, ``in``).
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in _IDENT_RE.findall(text):
        # Original form first (preserves CamelCase queries against FTS).
        if raw.lower() not in seen and len(raw) >= 3:
            seen.add(raw.lower())
            out.append(raw)
        # snake_case split.
        for part in raw.split("_"):
            if part and part.lower() not in seen and len(part) >= 3:
                seen.add(part.lower())
                out.append(part)
        # camelCase split.
        for part in _CAMEL_SPLIT_RE.split(raw):
            if part and part.lower() not in seen and len(part) >= 3:
                seen.add(part.lower())
                out.append(part)
    return out


class IndexQuery:
    """Read-only query interface over the file/symbol index.

    Opens the sqlite db in read-only mode. Safe to instantiate once per
    plan/architect call and discard.
    """

    def __init__(self, db_path: Path) -> None:
        _check_disabled()
        self._db_path = db_path
        self._conn = _open_reader(db_path)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "IndexQuery":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- queries -----------------------------------------------------------

    def search_symbols(self, query: str, limit: int = 30) -> list[SymbolHit]:
        """Search ``symbols_fts`` for *query*. Returns up to *limit* hits.

        Tries an exact-name match first (cheap index hit) and falls back
        to FTS5 prefix match for partial / multi-word queries. Empty
        query → ``[]``.
        """
        q = query.strip()
        if not q:
            return []

        out: list[SymbolHit] = []
        seen: set[tuple[str, str, int]] = set()

        # Exact name first.
        cur = self._conn.execute(
            "SELECT s.name, s.kind, s.file_path, s.line, s.signature "
            "FROM symbols s WHERE s.name=? LIMIT ?",
            (q, limit),
        )
        for row in cur.fetchall():
            key = (row[0], row[2], row[3])
            if key in seen:
                continue
            seen.add(key)
            out.append(
                SymbolHit(
                    name=row[0],
                    kind=row[1],
                    file_path=row[2],
                    line=row[3],
                    signature=row[4],
                )
            )

        if len(out) < limit:
            # FTS5 prefix MATCH. Sanitize to avoid syntax errors.
            fts_q = _to_fts_query(q)
            if fts_q:
                try:
                    cur = self._conn.execute(
                        "SELECT s.name, s.kind, s.file_path, s.line, s.signature "
                        "FROM symbols s "
                        "JOIN symbols_fts f ON s.id = f.rowid "
                        "WHERE symbols_fts MATCH ? "
                        "ORDER BY rank LIMIT ?",
                        (fts_q, limit),
                    )
                    for row in cur.fetchall():
                        key = (row[0], row[2], row[3])
                        if key in seen:
                            continue
                        seen.add(key)
                        out.append(
                            SymbolHit(
                                name=row[0],
                                kind=row[1],
                                file_path=row[2],
                                line=row[3],
                                signature=row[4],
                            )
                        )
                        if len(out) >= limit:
                            break
                except sqlite3.OperationalError as exc:
                    _log.debug(
                        "file_index.search_symbols_fts_err q=%r err=%s",
                        q,
                        str(exc),
                    )
        return out[:limit]

    def search_files(self, pattern: str, limit: int = 30) -> list[FileHit]:
        """Search the file inventory for *pattern* (substring on path).

        Tries a sqlite ``LIKE`` substring first, then FTS5 trigram fall-
        through for partial matches. Lowercase-insensitive.
        """
        pat = pattern.strip()
        if not pat:
            return []

        out: list[FileHit] = []
        seen: set[str] = set()

        # LIKE substring (cheap, exact).
        like = f"%{pat}%"
        cur = self._conn.execute(
            "SELECT path, lang FROM files WHERE path LIKE ? LIMIT ?",
            (like, limit),
        )
        for row in cur.fetchall():
            if row[0] in seen:
                continue
            seen.add(row[0])
            out.append(FileHit(path=row[0], lang=row[1]))

        if len(out) < limit:
            fts_q = _to_fts_query(pat)
            if fts_q:
                try:
                    cur = self._conn.execute(
                        "SELECT f.path FROM files_fts ff "
                        "JOIN files f ON f.path = ff.path "
                        "WHERE files_fts MATCH ? LIMIT ?",
                        (fts_q, limit),
                    )
                    for row in cur.fetchall():
                        if row[0] in seen:
                            continue
                        seen.add(row[0])
                        # Need the lang via a quick re-query.
                        lr = self._conn.execute(
                            "SELECT lang FROM files WHERE path=?",
                            (row[0],),
                        ).fetchone()
                        lang = lr[0] if lr else "other"
                        out.append(FileHit(path=row[0], lang=lang))
                        if len(out) >= limit:
                            break
                except sqlite3.OperationalError as exc:
                    _log.debug(
                        "file_index.search_files_fts_err q=%r err=%s",
                        pat,
                        str(exc),
                    )
        return out[:limit]

    def get_candidates_for_spec(
        self, spec_text: str, limit: int = 30
    ) -> CandidateDigest:
        """Aggregate candidate symbols + files for a spec text.

        Tokenizes *spec_text* into identifier tokens (camelCase / snake_case
        aware), runs FTS searches per token, deduplicates, scores by
        ``hit_count`` × ``(1 / (1 + rank))``, returns the top *limit*
        symbols and a parallel set of file hits.
        """
        tokens = _tokenize_spec(spec_text)
        if not tokens:
            return CandidateDigest()

        sym_score: dict[tuple[str, str, str, int, str], float] = {}
        file_score: dict[tuple[str, str], float] = {}

        for token in tokens:
            for hit in self.search_symbols(token, limit=10):
                key = (
                    hit.name,
                    hit.kind,
                    hit.file_path,
                    hit.line,
                    hit.signature,
                )
                sym_score[key] = sym_score.get(key, 0.0) + 1.0
                # Each symbol hit also implies its file is interesting.
                fkey = (hit.file_path, "")
                file_score[fkey] = file_score.get(fkey, 0.0) + 0.5
            for fhit in self.search_files(token, limit=5):
                file_key = (fhit.path, fhit.lang)
                file_score[file_key] = file_score.get(file_key, 0.0) + 1.0

        sym_hits = [
            SymbolHit(
                name=k[0], kind=k[1], file_path=k[2], line=k[3], signature=k[4]
            )
            for k, _ in sorted(
                sym_score.items(), key=lambda kv: (-kv[1], kv[0])
            )[:limit]
        ]
        file_hits_raw = sorted(
            file_score.items(), key=lambda kv: (-kv[1], kv[0])
        )
        # Deduplicate by path (different lang rows), prefer non-empty lang.
        file_hits: list[FileHit] = []
        seen_paths: set[str] = set()
        for (path, lang), _score in file_hits_raw:
            if path in seen_paths:
                continue
            seen_paths.add(path)
            if not lang:
                lr = self._conn.execute(
                    "SELECT lang FROM files WHERE path=?", (path,)
                ).fetchone()
                lang = lr[0] if lr else "other"
            file_hits.append(FileHit(path=path, lang=lang))
            if len(file_hits) >= limit:
                break

        truncated = len(sym_score) > limit or len(file_score) > limit
        return CandidateDigest(
            symbol_hits=sym_hits, file_hits=file_hits, truncated=truncated
        )

    def meta_summary(self) -> dict[str, str]:
        """Read the ``meta`` table — for ``autodev doctor`` / ``status``."""
        cur = self._conn.execute("SELECT key, value FROM meta")
        return {row[0]: row[1] for row in cur.fetchall()}


def _to_fts_query(text: str) -> str:
    """Sanitize *text* into a safe FTS5 ``MATCH`` argument.

    Strips quote chars, splits on whitespace, and re-joins each token as
    a prefix match (``foo*``). Empty / digit-only tokens are dropped.
    The result is suitable for both the ``unicode61`` and ``trigram``
    tokenizers used in our schema.
    """
    cleaned = re.sub(r'[\"\']', " ", text)
    parts = [t for t in cleaned.split() if t and not t.isdigit()]
    if not parts:
        return ""
    # Use simple OR so any token can match. Wrap in parens to keep
    # operator precedence stable.
    safe = []
    for p in parts:
        # Strip non-alnum-_ chars that would break trigram tokenizer.
        token = re.sub(r"[^A-Za-z0-9_]", "", p)
        if not token:
            continue
        safe.append(f"{token}*")
    if not safe:
        return ""
    return " OR ".join(safe)


__all__ = [
    "INDEX_SCHEMA_VERSION",
    "CandidateDigest",
    "FileHit",
    "IndexBuildContentionError",
    "IndexBuilder",
    "IndexDisabledError",
    "IndexQuery",
    "IndexStats",
    "SymbolHit",
    "_last_indexed_sha",
]


def _main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for the opt-in async (subprocess) full build.

    ``autodev init`` spawns ``python -m state.file_index build-full
    --cwd <repo> --db <db> [--workers N] [--batch-size N]`` for huge-repo
    async builds. Prior to this entrypoint the module had no ``__main__``
    block, so the subprocess loaded the module and exited doing nothing —
    the async path never indexed. This restores it.
    """
    import argparse

    ap = argparse.ArgumentParser(prog="state.file_index")
    sub = ap.add_subparsers(dest="command", required=True)
    bf = sub.add_parser("build-full", help="Full rebuild of the index.")
    bf.add_argument("--cwd", type=Path, required=True)
    bf.add_argument("--db", type=Path, required=True)
    bf.add_argument("--workers", type=int, default=0)
    bf.add_argument("--batch-size", type=int, default=1000)
    args = ap.parse_args(argv)

    if args.command == "build-full":
        logging.basicConfig(level=logging.INFO)
        stats = IndexBuilder.build_full(
            args.cwd,
            args.db,
            workers=args.workers,
            batch_size=args.batch_size,
        )
        _log.info(
            "file_index.build_full_done files=%d symbols=%d duration_ms=%d",
            stats.file_count,
            stats.symbol_count,
            stats.duration_ms,
        )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
