"""Shared scan-file collection for the syntax/build gates (G7 + S2).

Both :mod:`qa.syntax_check` and :mod:`qa.build_check` walk the repo tree with
``rglob`` to find source files. Two problems motivated this helper:

G7 (submodule + generated exclude)
----------------------------------
A whole-tree ``rglob`` descends into:

* *submodule* checkouts — a submodule's syntax/lint failure is not the
  executor's concern and must not block an unrelated fix;
* ``.git`` / ``.git/modules`` internal copies — never source the task touched;
* *generated* code (``*_pb2.py``, ``*_pb2_grpc.py`` protobuf/gRPC stubs) — a
  syntax error in regenerated output should not block the host fix.

:func:`collect_scan_files` excludes all of the above via a shared
``_SKIP_DIRS`` guard, a ``.gitmodules`` parse, and generated-name globs.

S2 / WS2-16 (diff-scoping)
--------------------------
The whole-tree walk is O(repo) on every task. :func:`collect_scan_files`
accepts an optional ``paths`` parameter (repo-relative changed files). When
``paths`` is given the scan is scoped to those files (still excluding generated
files even when explicitly listed). When ``paths`` is ``None`` the whole-tree
walk is preserved (back-compat) but with the skip-dir exclusions applied.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

# Directory *names* that are never task source: VCS internals, virtualenvs,
# vendored deps, caches, and build output. A single ``part in _SKIP_DIRS``
# check (the established secretscan pattern) excludes the whole subtree.
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",  # also covers .git/modules submodule checkout copies
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".tox",
        "dist",
        "build",
    }
)

# Generated-file name globs (matched against the *file name*, not the full
# path). protobuf and gRPC stub outputs are the common offenders; a syntax
# error in regenerated output must not block the host fix.
_GENERATED_FILE_GLOBS: tuple[str, ...] = (
    "*_pb2.py",
    "*_pb2_grpc.py",
    "*_pb2.pyi",
)

# Directory *names* that hold generated stubs (OpenAPI / gRPC codegen output).
# Whole subtree excluded like _SKIP_DIRS.
_GENERATED_DIRS: frozenset[str] = frozenset(
    {
        "__generated__",
        "_generated",
        "generated",
        "gen",
        "openapi_client",
        "grpc_generated",
    }
)


def _parse_gitmodules(cwd: Path) -> set[str]:
    """Return the set of submodule paths (repo-relative, posix) from ``.gitmodules``.

    Parses ``path = <dir>`` lines. Tolerant of missing file / read errors —
    returns an empty set so the caller degrades to "no submodules" rather than
    crashing the gate. We avoid a TOML/ini dependency: the lightweight scan
    matches git's own ``[submodule "..."]`` + ``path = ...`` layout.
    """
    path = cwd / ".gitmodules"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    out: set[str] = set()
    for raw in text.splitlines():
        stripped = raw.strip()
        # ``path = vendor/foo`` (whitespace around ``=`` is tolerated by git).
        if stripped.startswith("path") and "=" in stripped:
            key, _, value = stripped.partition("=")
            if key.strip() == "path":
                sub = value.strip().strip('"').rstrip("/")
                if sub:
                    out.add(Path(sub).as_posix())
    return out


def _is_generated_name(name: str) -> bool:
    """True iff *name* matches a known generated-file glob."""
    return any(fnmatch.fnmatch(name, glob) for glob in _GENERATED_FILE_GLOBS)


def _rel_under_submodule(rel_posix: str, submodules: set[str]) -> bool:
    """True iff *rel_posix* is at or under any submodule path."""
    for sub in submodules:
        if rel_posix == sub or rel_posix.startswith(sub + "/"):
            return True
    return False


def _is_excluded(item: Path, cwd: Path, submodules: set[str]) -> bool:
    """Return True iff *item* should be excluded from the scan (G7 guard).

    Excludes: skip-dirs (``.git``, ``.venv``, …), generated stub dirs,
    submodule subtrees (from ``.gitmodules``), and generated file names.
    """
    parts = item.parts
    if any(part in _SKIP_DIRS for part in parts):
        return True
    if any(part in _GENERATED_DIRS for part in parts):
        return True
    if _is_generated_name(item.name):
        return True
    if submodules:
        try:
            rel_posix = item.relative_to(cwd).as_posix()
        except ValueError:
            rel_posix = item.as_posix()
        if _rel_under_submodule(rel_posix, submodules):
            return True
    return False


def collect_scan_files(
    cwd: Path,
    suffixes: tuple[str, ...],
    *,
    paths: list[Path] | None = None,
) -> list[Path]:
    """Collect source files under *cwd* whose suffix is in *suffixes*.

    G7: submodule subtrees (``.gitmodules``), ``.git``/``.git/modules``,
    vendored/cache/build dirs (:data:`_SKIP_DIRS`), generated stub dirs, and
    generated file names (``*_pb2.py`` …) are excluded in *both* modes.

    S2: when *paths* is non-``None`` the scan is scoped to those files
    (resolved relative to *cwd*); a file outside *paths* is never scanned.
    Generated files are skipped even when explicitly listed. When *paths* is
    ``None`` the whole tree is walked (back-compat) with the exclusions above.
    An empty *paths* list scans nothing (the "no files in diff scope" no-op).
    """
    submodules = _parse_gitmodules(cwd)
    suffix_set = {s.lower() for s in suffixes}

    if paths is None:
        out: list[Path] = []
        for item in cwd.rglob("*"):
            try:
                if not item.is_file():
                    continue
            except OSError:
                continue
            if item.suffix.lower() not in suffix_set:
                continue
            if _is_excluded(item, cwd, submodules):
                continue
            out.append(item)
        return out

    # Diff-scoped mode (S2). Resolve each path relative to cwd; skip
    # nonexistent paths and generated/excluded files silently.
    seen: set[Path] = set()
    scoped: list[Path] = []
    for raw in paths:
        candidate = raw if raw.is_absolute() else (cwd / raw)
        try:
            resolved = candidate.resolve()
        except (OSError, ValueError):
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.suffix.lower() not in suffix_set:
            continue
        try:
            if not resolved.is_file():
                continue
        except OSError:
            continue
        if _is_excluded(resolved, cwd, submodules):
            continue
        scoped.append(resolved)
    return scoped


__all__ = ["collect_scan_files"]
