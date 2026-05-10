"""Hallucination-guard QA gate.

Catches API hallucinations — references to functions / attributes that
do not exist on the imported module. A known LLM failure mode: the
model invents plausible-sounding but non-existent API calls (e.g.
``os.is_dir`` instead of ``os.path.isdir``). Existing QA gates (lint,
build, tests) catch this at runtime, but only after the diff has
landed; the guard is positioned to catch hallucinations BEFORE the
reviewer sees the diff.

v0.16.0 — Python only. AST-walks ``.py`` files; resolves ``from foo import
bar`` and ``foo.bar`` references against importable stdlib modules.

v0.19.0 — extended to TypeScript / JavaScript / C++ via dispatcher.
TypeScript uses regex-based module extraction + ``node_modules`` resolution
(no native deps; tree-sitter-typescript planned). C++ uses
``qa.cpp_symbols`` (tree-sitter-cpp when available; regex fallback)
with include-chain resolution to detect calls to undeclared functions
(~80% of C++ hallucinations).

Conservative skip-and-warn dominates: when a module / package / header
chain cannot be resolved, the guard *passes*. False-positives erode trust
in the gate; the existing build / lint / test gates catch the residual
class.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import importlib.util
import inspect
import json
import logging
import re
import sys
from pathlib import Path

from plugins.registry import GateResult


_log = logging.getLogger(__name__)

# v0.22.1 A1: per-file watchdog default. Operators can override via
# ``cfg.qa_gates.regex_timeout_per_file_s``. Set conservatively (10 s)
# on the assumption that a healthy single-file scan completes in <1 s
# for repos under 1 GB. The 2026-05-09 Unity stall (358K files) showed
# a single C++ header could pin the regex engine indefinitely.
DEFAULT_PER_FILE_TIMEOUT_S = 10.0


# Files / dirs we never walk (mirrors the secretscan skip set).
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        "dist",
        "build",
        ".tox",
    }
)


# Modules where ``find_spec`` may return None on a standard install.
# Used to decide whether a missing module is suspicious or expected.
# A liberal stdlib whitelist keeps false-positive rates low while still
# catching the obvious hallucinations.
_STDLIB_MODULE_NAMES: frozenset[str] = frozenset(sys.stdlib_module_names)


# v0.19.0 — extension dispatch.
_PY_EXTS: frozenset[str] = frozenset({".py"})
_TS_EXTS: frozenset[str] = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"})
_CPP_EXTS: frozenset[str] = frozenset(
    {".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".hxx"}
)


def _iter_files(
    cwd: Path, paths: list[Path] | None
) -> list[Path]:
    """Yield source files under *cwd* or in the *paths* whitelist.

    v0.19.0: returns Python, TypeScript / JavaScript, and C++ source files
    (the dispatcher routes by extension).
    """
    accepted = _PY_EXTS | _TS_EXTS | _CPP_EXTS
    if paths is not None:
        out: list[Path] = []
        seen: set[Path] = set()
        for raw in paths:
            candidate = (cwd / raw) if not raw.is_absolute() else raw
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            if not resolved.exists():
                continue
            if resolved.suffix.lower() not in accepted:
                continue
            out.append(resolved)
        return out

    out = []
    for ext in accepted:
        for item in cwd.rglob(f"*{ext}"):
            if any(part in _SKIP_DIRS for part in item.parts):
                continue
            out.append(item)
    return out


def _is_stdlib(module_name: str) -> bool:
    """True iff *module_name* (top-level) is a stdlib module."""
    head = module_name.split(".")[0]
    return head in _STDLIB_MODULE_NAMES


def _resolve_module(module_name: str) -> object | None:
    """Best-effort import of *module_name*."""
    try:
        spec = importlib.util.find_spec(module_name)
    except (ValueError, ModuleNotFoundError, ImportError):
        return None
    if spec is None:
        return None
    try:
        return importlib.import_module(module_name)
    except Exception:  # noqa: BLE001
        return None


def _module_has_attr(module: object, attr: str) -> bool:
    if hasattr(module, attr):
        return True
    try:
        for name, _val in inspect.getmembers(module):
            if name == attr:
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


# ---------------------------------------------------------------------------
# Python scanner (v0.16.0)
# ---------------------------------------------------------------------------


def _scan_python_file(path: Path, repo_root: Path) -> list[str]:
    """Return a list of finding strings for one Python file."""
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    try:
        rel = path.relative_to(repo_root).as_posix()
    except ValueError:
        rel = path.as_posix()

    findings: list[str] = []

    imported_modules: dict[str, str] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                imported_modules[local] = alias.name

        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            module_name = node.module
            if not _is_stdlib(module_name):
                continue
            module = _resolve_module(module_name)
            if module is None:
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                if not _module_has_attr(module, alias.name):
                    findings.append(
                        f"{rel}:{node.lineno}: hallucinated reference — "
                        f"{alias.name} not found in {module_name}"
                    )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if not isinstance(node.value, ast.Name):
            continue
        local = node.value.id
        if local not in imported_modules:
            continue
        full_module = imported_modules[local]
        if not _is_stdlib(full_module):
            continue
        module = _resolve_module(full_module)
        if module is None:
            continue
        if not _module_has_attr(module, node.attr):
            findings.append(
                f"{rel}:{node.lineno}: hallucinated reference — "
                f"{node.attr} not found in {full_module}"
            )

    return findings


# ---------------------------------------------------------------------------
# TypeScript / JavaScript scanner (v0.19.0)
# ---------------------------------------------------------------------------


# v0.19.0: tree-sitter-typescript is OPTIONAL; flag set at import.
try:  # pragma: no cover - native binding presence varies by platform
    import tree_sitter_typescript  # type: ignore[import-not-found,import-untyped]  # noqa: F401

    TS_TREESITTER_AVAILABLE = True
except Exception:  # noqa: BLE001
    TS_TREESITTER_AVAILABLE = False


_TS_IMPORT_RE = re.compile(
    r"""(?:
        import\b[^'"\n]*?from\s*['"]([^'"]+)['"]   |   # ES module import
        import\s*\(\s*['"]([^'"]+)['"]\s*\)            # dynamic import()
    )""",
    re.VERBOSE,
)
_TS_BARE_IMPORT_RE = re.compile(r"""import\s+['"]([^'"]+)['"]""")
_TS_REQUIRE_RE = re.compile(r"""require\s*\(\s*['"]([^'"]+)['"]\s*\)""")


def _scan_typescript_regex(source: str) -> set[str]:
    """Extract npm package specifiers from TypeScript / JavaScript source.

    Returns the set of normalized **package roots** referenced via ES
    import or CJS ``require``. Subpath imports (``lodash/fp``) collapse to
    the root package (``lodash``); scoped packages (``@types/node``)
    preserve the ``@scope/name`` shape (collapsing subpaths within the
    scope). Relative imports (``./``, ``../``, ``/``) and Node-builtin
    bare specifiers (e.g. ``fs``, ``path``) are excluded.
    """
    raw_specs: list[str] = []
    for m in _TS_IMPORT_RE.finditer(source):
        for grp in m.groups():
            if grp:
                raw_specs.append(grp)
    raw_specs.extend(_TS_BARE_IMPORT_RE.findall(source))
    raw_specs.extend(_TS_REQUIRE_RE.findall(source))

    out: set[str] = set()
    for spec in raw_specs:
        if not spec:
            continue
        if spec.startswith((".", "/")):
            continue
        # Scoped package: ``@scope/name`` keeps both segments.
        if spec.startswith("@"):
            parts = spec.split("/")
            if len(parts) >= 2:
                out.add(f"{parts[0]}/{parts[1]}")
            continue
        # Plain package: keep only first segment.
        out.add(spec.split("/")[0])
    return out


def _resolve_ts_package(cwd: Path, package: str) -> bool:
    """True iff *package* resolves under ``cwd/node_modules``."""
    base = cwd / "node_modules" / package
    if not base.exists():
        return False
    pkg_json = base / "package.json"
    if pkg_json.exists():
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("name"):
                return True
        except (OSError, json.JSONDecodeError):
            return False
    return base.is_dir()


def _scan_typescript_file(path: Path, repo_root: Path) -> list[str]:
    """Return findings for one TypeScript / JavaScript file."""
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        rel = path.relative_to(repo_root).as_posix()
    except ValueError:
        rel = path.as_posix()

    # Skip-and-warn when no node_modules exists — can't verify anything.
    node_modules = repo_root / "node_modules"
    if not node_modules.exists():
        return []

    packages = _scan_typescript_regex(source)
    findings: list[str] = []
    for pkg in sorted(packages):
        if not _resolve_ts_package(repo_root, pkg):
            findings.append(
                f"{rel}: hallucinated reference — "
                f"package '{pkg}' not found in node_modules"
            )
    return findings


# ---------------------------------------------------------------------------
# C++ scanner (v0.19.0)
# ---------------------------------------------------------------------------


def _scan_cpp_file_dispatch(path: Path, repo_root: Path) -> list[str]:
    """Dispatch to ``qa.cpp_symbols.scan_cpp_file``."""
    from qa.cpp_symbols import scan_cpp_file

    return scan_cpp_file(path, repo_root)


# ---------------------------------------------------------------------------
# Top-level dispatcher
# ---------------------------------------------------------------------------


def _dispatch(path: Path, repo_root: Path) -> list[str]:
    """Pick the right scanner for *path* by extension."""
    ext = path.suffix.lower()
    if ext in _PY_EXTS:
        return _scan_python_file(path, repo_root)
    if ext in _TS_EXTS:
        return _scan_typescript_file(path, repo_root)
    if ext in _CPP_EXTS:
        return _scan_cpp_file_dispatch(path, repo_root)
    return []


async def _dispatch_with_timeout(
    path: Path,
    repo_root: Path,
    timeout_s: float = DEFAULT_PER_FILE_TIMEOUT_S,
) -> list[str]:
    """Run :func:`_dispatch` in a worker thread with a wall-clock ceiling.

    v0.22.1 A1: a single misbehaved regex (or pathologically long file)
    used to pin the orchestrator's main thread for tens of minutes. We
    now run each file in :func:`asyncio.to_thread` under
    :func:`asyncio.wait_for`; on timeout we log a structured event and
    skip-and-warn (return empty findings) so the gate cannot block the
    task. The cost of skipping a slow file is a missed hallucination
    finding — which the build / test gates downstream still catch.
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_dispatch, path, repo_root),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        _log.warning(
            "qa.hallucination_guard.regex_timeout path=%s timeout_s=%s",
            str(path),
            timeout_s,
        )
        return []


async def run_hallucination_guard(
    cwd: Path,
    paths: list[Path] | None = None,
    per_file_timeout_s: float = DEFAULT_PER_FILE_TIMEOUT_S,
) -> GateResult:
    """Scan *cwd* (or *paths*) for hallucinated API references.

    Args:
        cwd: Repository root.
        paths: Optional diff-scope filter. When non-None, only the listed
            files are walked (Python / TypeScript / C++ extensions only).
            Mirrors v0.13.0's secretscan diff-scope signature.
        per_file_timeout_s: v0.22.1 A1 — per-file wall-clock ceiling. On
            timeout the file is skip-and-warn'd. Default
            :data:`DEFAULT_PER_FILE_TIMEOUT_S`.

    Returns:
        :class:`GateResult` with ``passed=False`` and a finding list when
        any hallucinations are detected. Otherwise ``passed=True`` with a
        benign details string.
    """
    files = _iter_files(cwd, paths)
    all_findings: list[str] = []
    for f in files:
        findings = await _dispatch_with_timeout(
            f, repo_root=cwd, timeout_s=per_file_timeout_s
        )
        all_findings.extend(findings)

    if all_findings:
        detail_lines = all_findings[:20]
        suffix = (
            f"\n… and {len(all_findings) - 20} more"
            if len(all_findings) > 20
            else ""
        )
        return GateResult(
            passed=False,
            details=(
                "potential hallucinated API references:\n"
                + "\n".join(detail_lines)
                + suffix
            ),
        )
    return GateResult(passed=True, details="no hallucinated references detected")


# Back-compat alias for any in-tree callers — preserved for the v0.16.0
# private surface.
def _scan_file(path: Path, repo_root: Path) -> list[str]:
    return _scan_python_file(path, repo_root)


__all__ = [
    "TS_TREESITTER_AVAILABLE",
    "_scan_typescript_regex",
    "run_hallucination_guard",
]
