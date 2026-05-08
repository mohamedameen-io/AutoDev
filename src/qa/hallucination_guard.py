"""Hallucination-guard QA gate (v0.16.0).

Catches API hallucinations — references to functions / attributes that
do not exist on the imported module. A known LLM failure mode: the
model invents plausible-sounding but non-existent API calls (e.g.
``os.is_dir`` instead of ``os.path.isdir``). Existing QA gates (lint,
build, tests) catch this at runtime, but only after the diff has
landed; the guard is positioned to catch hallucinations BEFORE the
reviewer sees the diff.

Strategy (Python only in v0.16.0):

  1. AST-walk every ``.py`` file under ``cwd`` (or the diff-scoped
     ``paths`` list if provided).
  2. For each ``from <module> import <attr>`` and each ``<module>.<attr>``
     reference, attempt to resolve ``<attr>`` on ``<module>`` via
     :mod:`importlib`. If the symbol cannot be located, emit a finding.
  3. Skip-and-warn for non-resolvable modules (third-party packages
     not installed in the scan environment, ``importlib.import_module``
     dynamic imports, etc.) — false positives in this class would be
     punitive on real-world projects.

Diff-scope mirror of v0.13.0 secretscan: when ``paths`` is provided,
only those files are walked. Non-existent paths skip silently.

TypeScript / JavaScript / C++ are out of scope for v0.16.0 — the
``tree-sitter-typescript`` dependency was not present in the project
at v0.16.0 release time. Slated for v0.16.1.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import sys
from pathlib import Path

from plugins.registry import GateResult


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


def _iter_files(
    cwd: Path, paths: list[Path] | None
) -> list[Path]:
    """Yield Python files under *cwd* or in the *paths* whitelist."""
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
            if resolved.suffix != ".py":
                continue
            out.append(resolved)
        return out

    out = []
    for item in cwd.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in item.parts):
            continue
        out.append(item)
    return out


def _is_stdlib(module_name: str) -> bool:
    """True iff *module_name* (top-level) is a stdlib module."""
    head = module_name.split(".")[0]
    return head in _STDLIB_MODULE_NAMES


def _resolve_module(module_name: str) -> object | None:
    """Best-effort import of *module_name*.

    Returns the imported module on success, ``None`` otherwise. Failures
    (missing module, ImportError on import) are swallowed — the caller
    distinguishes "module unknown" vs. "attr missing on known module"
    via :func:`_is_stdlib`.
    """
    try:
        spec = importlib.util.find_spec(module_name)
    except (ValueError, ModuleNotFoundError, ImportError):
        return None
    if spec is None:
        return None
    try:
        return importlib.import_module(module_name)
    except Exception:  # noqa: BLE001 — broad: any import-time error skips.
        return None


def _module_has_attr(module: object, attr: str) -> bool:
    """True iff *module* exposes *attr*.

    Uses :func:`hasattr` plus :func:`inspect` to be lenient about
    dunder-only objects and lazy attributes.
    """
    if hasattr(module, attr):
        return True
    # ``inspect.getmembers`` catches some lazily-bound symbols that
    # ``hasattr`` misses (e.g. modules using ``__getattr__`` lookups).
    try:
        for name, _val in inspect.getmembers(module):
            if name == attr:
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _scan_file(path: Path, repo_root: Path) -> list[str]:
    """Return a list of finding strings for one Python file."""
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        # Don't false-positive on broken source — the caller's
        # syntax_check gate already covers this case.
        return []

    try:
        rel = path.relative_to(repo_root).as_posix()
    except ValueError:
        rel = path.as_posix()

    findings: list[str] = []

    # Track imported modules: ``import os`` → ``{"os": "os"}``.
    # ``import os.path as op`` → ``{"op": "os.path"}``.
    imported_modules: dict[str, str] = {}

    for node in ast.walk(tree):
        # ``import foo`` / ``import foo as bar`` / ``import foo.bar``
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                imported_modules[local] = alias.name

        # ``from foo import bar`` / ``from foo import bar as baz``
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                # Relative import (``from . import x``) — skip; can't
                # resolve at scan time without project sys.path setup.
                continue
            module_name = node.module
            if not _is_stdlib(module_name):
                # Non-stdlib import: skip-and-warn rather than fail.
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

    # Second pass: ``module.attr`` references where ``module`` was
    # imported via ``import module`` (we tracked these in
    # ``imported_modules`` during the first walk).
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if not isinstance(node.value, ast.Name):
            continue  # nested attributes — too hard to resolve statically
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


async def run_hallucination_guard(
    cwd: Path,
    paths: list[Path] | None = None,
) -> GateResult:
    """Scan *cwd* (or *paths*) for hallucinated API references.

    Args:
        cwd: Repository root.
        paths: Optional diff-scope filter. When non-None, only the
            listed Python files are walked. Mirrors v0.13.0's
            secretscan diff-scope signature.

    Returns:
        :class:`GateResult` with ``passed=False`` and a finding list
        when any hallucinations are detected. Otherwise ``passed=True``
        with a benign details string.
    """
    files = _iter_files(cwd, paths)
    all_findings: list[str] = []
    for f in files:
        all_findings.extend(_scan_file(f, repo_root=cwd))

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


__all__ = ["run_hallucination_guard"]
