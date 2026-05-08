"""Tests for the v0.16.0 Python hallucination-guard.

The guard AST-walks Python source files, finds ``from module import attr``
and ``module.attr`` references, and verifies each ``attr`` resolves on
``module``. Hallucinated references emit findings with ``<file>:<line>:
hallucinated reference — {symbol} not found in {module}``.

Coverage:
  * Valid stdlib imports pass.
  * Hallucinated attr in ``from x import y`` form fails.
  * Module-attribute reference (``os.nonexistent_func``) fails.
  * Dynamic / unresolvable imports skip with warning (don't fail).
  * Diff-scope filter (``paths=`` parameter) restricts the walk.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qa.hallucination_guard import run_hallucination_guard


# ── helpers ────────────────────────────────────────────────────────────────


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ── valid imports ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_python_valid_import_passes(tmp_path: Path) -> None:
    """A file that imports real stdlib symbols should pass."""
    _write(
        tmp_path / "src" / "good.py",
        "from os import path\nfrom json import dumps\n"
        "import sys\nprint(sys.version)\n",
    )
    out = await run_hallucination_guard(tmp_path)
    assert out.passed is True


# ── hallucinated attrs ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_python_hallucinated_attr_in_import_fails(tmp_path: Path) -> None:
    """``from os import nonexistent_func`` → finding."""
    _write(
        tmp_path / "src" / "bad.py",
        "from os import nonexistent_func\nnonexistent_func()\n",
    )
    out = await run_hallucination_guard(tmp_path)
    assert out.passed is False
    assert "nonexistent_func" in out.details
    assert "os" in out.details
    assert "src/bad.py" in out.details


@pytest.mark.asyncio
async def test_python_hallucinated_attr_in_module_call_fails(tmp_path: Path) -> None:
    """``import os; os.nonexistent_func()`` → finding."""
    _write(
        tmp_path / "src" / "bad.py",
        "import os\nos.nonexistent_func()\n",
    )
    out = await run_hallucination_guard(tmp_path)
    assert out.passed is False
    assert "nonexistent_func" in out.details


@pytest.mark.asyncio
async def test_python_multiple_findings_aggregated(tmp_path: Path) -> None:
    """The guard surfaces every hallucinated reference, not just the first."""
    _write(
        tmp_path / "src" / "many.py",
        "from os import nonexistent_a\n"
        "import sys\n"
        "sys.totally_made_up()\n",
    )
    out = await run_hallucination_guard(tmp_path)
    assert out.passed is False
    assert "nonexistent_a" in out.details
    assert "totally_made_up" in out.details


# ── known stdlib module hallucinations ────────────────────────────────────


@pytest.mark.asyncio
async def test_python_hallucinated_unknown_module_skipped(tmp_path: Path) -> None:
    """Imports from non-stdlib non-installed modules degrade gracefully.

    Skip-and-warn rather than fail (per plan): if ``find_spec`` returns
    None for a non-known-stdlib namespace, the scan environment may
    simply lack the package. We don't want to false-positive on
    legitimate user code that uses third-party libs not installed in
    AutoDev's venv.
    """
    _write(
        tmp_path / "src" / "thirdparty.py",
        "from some_unknown_pkg import foo\nfoo()\n",
    )
    out = await run_hallucination_guard(tmp_path)
    # Pass — the unknown package was skipped, not flagged as a finding.
    assert out.passed is True


# ── diff-scope filter ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_python_paths_filter_works(tmp_path: Path) -> None:
    """``paths=`` restricts the walk to listed files only.

    Mirrors v0.13.0's secretscan diff-scope. A bad file outside the
    paths list is invisible to the scan.
    """
    _write(
        tmp_path / "src" / "in_diff.py",
        "from os import path\n",  # clean
    )
    _write(
        tmp_path / "src" / "outside_diff.py",
        "from os import nonexistent_func\n",  # bad but out of scope
    )
    out = await run_hallucination_guard(
        tmp_path, paths=[Path("src/in_diff.py")]
    )
    assert out.passed is True


@pytest.mark.asyncio
async def test_python_paths_filter_finds_finding_in_scope(tmp_path: Path) -> None:
    """When the bad file IS in the diff scope, the finding surfaces."""
    _write(
        tmp_path / "src" / "in_diff.py",
        "from os import nonexistent_func\n",
    )
    out = await run_hallucination_guard(
        tmp_path, paths=[Path("src/in_diff.py")]
    )
    assert out.passed is False
    assert "nonexistent_func" in out.details


@pytest.mark.asyncio
async def test_python_paths_filter_skips_missing_files(tmp_path: Path) -> None:
    """Non-existent paths in the list are silently skipped, not crashed."""
    _write(
        tmp_path / "src" / "exists.py",
        "import os\n",
    )
    out = await run_hallucination_guard(
        tmp_path,
        paths=[Path("src/exists.py"), Path("src/does_not_exist.py")],
    )
    assert out.passed is True


# ── dynamic / non-resolvable imports ───────────────────────────────────────


@pytest.mark.asyncio
async def test_python_skips_dynamic_imports_with_warning(tmp_path: Path) -> None:
    """``importlib.import_module(...)`` patterns are not statically
    resolvable; the guard must NOT flag them as findings."""
    _write(
        tmp_path / "src" / "dyn.py",
        "import importlib\n"
        "mod = importlib.import_module('os')\n"
        "func = getattr(mod, 'path')\n",
    )
    out = await run_hallucination_guard(tmp_path)
    assert out.passed is True


# ── syntax errors / non-Python files ──────────────────────────────────────


@pytest.mark.asyncio
async def test_python_syntax_error_does_not_crash_gate(tmp_path: Path) -> None:
    """A syntactically invalid Python file is skipped, not surfaced as a finding."""
    _write(
        tmp_path / "src" / "broken.py",
        "from os import (this is not valid python syntax\n",
    )
    out = await run_hallucination_guard(tmp_path)
    # Pass — broken file silently skipped (caller's lint / syntax_check
    # gate is the canonical signal for this class of error).
    assert out.passed is True


@pytest.mark.asyncio
async def test_python_non_python_files_ignored(tmp_path: Path) -> None:
    """Files that aren't ``.py`` are not opened by the guard at all."""
    _write(
        tmp_path / "src" / "notes.txt",
        "from os import nonexistent_func\n",  # not Python — must be ignored
    )
    out = await run_hallucination_guard(tmp_path)
    assert out.passed is True
