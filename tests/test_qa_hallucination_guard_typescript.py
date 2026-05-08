"""Tests for v0.19.0 TypeScript/JavaScript hallucination-guard support."""

from __future__ import annotations

from pathlib import Path

import pytest

from qa.hallucination_guard import run_hallucination_guard, _scan_typescript_regex


def test_regex_extracts_imports_from_source() -> None:
    src = '''
    import { foo } from "lodash";
    import bar from 'react';
    const x = require("express");
    '''
    mods = _scan_typescript_regex(src)
    assert "lodash" in mods
    assert "react" in mods
    assert "express" in mods


def test_regex_skips_relative_imports() -> None:
    """Relative imports (./, ../) are not module-resolution candidates."""
    src = '''
    import { foo } from "./local";
    import bar from "../sibling";
    import baz from "/absolute";
    '''
    mods = _scan_typescript_regex(src)
    assert "./local" not in mods
    assert "../sibling" not in mods
    # Absolute paths likewise: caller resolves via node_modules.
    assert mods == set() or all(not m.startswith(".") for m in mods)


def test_regex_strips_subpath_imports() -> None:
    """``lodash/fp`` resolves to package ``lodash``."""
    src = '''
    import { fp } from "lodash/fp";
    import scoped from "@types/node/fs";
    '''
    mods = _scan_typescript_regex(src)
    # Subpaths normalize to root package.
    assert "lodash" in mods
    # Scoped packages keep the @scope/name root.
    assert "@types/node" in mods


@pytest.mark.asyncio
async def test_typescript_clean_repo_passes(tmp_path: Path) -> None:
    """TS file with no imports → no findings."""
    (tmp_path / "app.ts").write_text("const x = 1;\nconsole.log(x);\n")
    result = await run_hallucination_guard(tmp_path)
    assert result.passed


@pytest.mark.asyncio
async def test_typescript_missing_node_modules_warns(tmp_path: Path) -> None:
    """When ``node_modules`` is absent, treat as skip-and-warn (pass)."""
    (tmp_path / "app.ts").write_text(
        'import { unknown } from "totally-fake-package";\n'
    )
    result = await run_hallucination_guard(tmp_path)
    # No node_modules → can't verify; gate stays clean.
    assert result.passed


@pytest.mark.asyncio
async def test_typescript_resolved_package_passes(tmp_path: Path) -> None:
    """A package present in ``node_modules`` produces no finding."""
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "lodash").mkdir()
    (tmp_path / "node_modules" / "lodash" / "package.json").write_text(
        '{"name": "lodash", "version": "4.17.21"}'
    )
    (tmp_path / "app.ts").write_text(
        'import { foo } from "lodash";\nconsole.log(foo);\n'
    )
    result = await run_hallucination_guard(tmp_path)
    assert result.passed


@pytest.mark.asyncio
async def test_typescript_unresolved_package_with_node_modules_warns(
    tmp_path: Path,
) -> None:
    """node_modules present but module missing → finding."""
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "lodash").mkdir()
    (tmp_path / "node_modules" / "lodash" / "package.json").write_text(
        '{"name": "lodash"}'
    )
    (tmp_path / "app.ts").write_text(
        'import { foo } from "ghost-package-xyz";\n'
    )
    result = await run_hallucination_guard(tmp_path)
    assert not result.passed
    assert "ghost-package-xyz" in result.details


@pytest.mark.asyncio
async def test_typescript_dispatch_jsx_files(tmp_path: Path) -> None:
    """``.jsx`` and ``.tsx`` extensions are scanned."""
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "react").mkdir()
    (tmp_path / "node_modules" / "react" / "package.json").write_text(
        '{"name": "react"}'
    )
    (tmp_path / "App.tsx").write_text(
        'import React from "react";\nimport { Missing } from "ghost-pkg";\n'
    )
    result = await run_hallucination_guard(tmp_path)
    assert not result.passed
    assert "ghost-pkg" in result.details


@pytest.mark.asyncio
async def test_typescript_require_pattern_resolved(tmp_path: Path) -> None:
    """CommonJS ``require("…")`` is scanned."""
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "fs-extra").mkdir()
    (tmp_path / "node_modules" / "fs-extra" / "package.json").write_text(
        '{"name": "fs-extra"}'
    )
    (tmp_path / "app.js").write_text(
        'const fs = require("fs-extra");\nconst missing = require("nope-pkg");\n'
    )
    result = await run_hallucination_guard(tmp_path)
    assert not result.passed
    assert "nope-pkg" in result.details


@pytest.mark.asyncio
async def test_typescript_relative_imports_ignored(tmp_path: Path) -> None:
    """Relative imports are not module-resolution candidates."""
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "app.ts").write_text(
        'import { local } from "./helper";\n'
        'import { sibling } from "../shared";\n'
    )
    result = await run_hallucination_guard(tmp_path)
    assert result.passed
