"""v0.14.0 ``run_secretscan(edit_scope=...)`` composition tests.

Builds on v0.13.0's ``paths=`` diff-scope filter:

* When ``edit_scope`` is non-empty AND ``paths`` is ``None``, the scanner
  still walks the tree but restricts to files under the scope prefixes.
  This catches secrets in not-yet-diffed files (e.g. a fresh dev run that
  forgot to commit) without polluting findings with unrelated repo state.

* When BOTH ``paths`` and ``edit_scope`` are set, the filters compose:
  the file must be in BOTH the diff AND the scope to be scanned.

* When ``edit_scope`` is empty / ``None``, behavior is identical to
  v0.13.0 — full walk or diff-paths scope.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qa.secretscan import run_secretscan


@pytest.mark.asyncio
async def test_secretscan_with_edit_scope_only_walks_scope(tmp_path: Path) -> None:
    """``edit_scope=['src']`` and ``paths=None`` → walk under src/ only."""
    (tmp_path / "src").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "src" / "in_scope.py").write_text(
        "KEY = 'AKIAIOSFODNN7EXAMPLE'\n"
    )
    # Out-of-scope secret should be IGNORED.
    (tmp_path / "docs" / "out.py").write_text(
        "OTHER = 'AKIAQQQQQQQQQQQQQQQQ'\n"
    )

    result = await run_secretscan(tmp_path, edit_scope=["src"])
    # The in-scope file's secret IS surfaced.
    assert not result.passed
    assert "src/in_scope.py" in result.details
    # The out-of-scope file's secret is NOT surfaced.
    assert "docs/out.py" not in result.details


@pytest.mark.asyncio
async def test_secretscan_with_empty_edit_scope_preserves_legacy_walk(
    tmp_path: Path,
) -> None:
    """``edit_scope=[]`` (empty / legacy) → no constraint added; full walk."""
    (tmp_path / "anywhere.py").write_text(
        "KEY = 'AKIAIOSFODNN7EXAMPLE'\n"
    )
    result = await run_secretscan(tmp_path, edit_scope=[])
    assert not result.passed


@pytest.mark.asyncio
async def test_secretscan_with_none_edit_scope_preserves_legacy_walk(
    tmp_path: Path,
) -> None:
    """``edit_scope=None`` (default) → no constraint added; full walk."""
    (tmp_path / "anywhere.py").write_text(
        "KEY = 'AKIAIOSFODNN7EXAMPLE'\n"
    )
    result = await run_secretscan(tmp_path)
    assert not result.passed


@pytest.mark.asyncio
async def test_secretscan_diff_paths_intersected_with_edit_scope(
    tmp_path: Path,
) -> None:
    """When BOTH ``paths`` AND ``edit_scope`` are set, scanner only
    considers files in the intersection.

    paths = [src/foo.py, docs/bar.py]
    edit_scope = [src]
    → only src/foo.py is scanned.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "src" / "foo.py").write_text(
        "KEY = 'AKIAIOSFODNN7EXAMPLE'\n"
    )
    (tmp_path / "docs" / "bar.py").write_text(
        "OTHER = 'AKIAQQQQQQQQQQQQQQQQ'\n"
    )

    result = await run_secretscan(
        tmp_path,
        paths=[Path("src/foo.py"), Path("docs/bar.py")],
        edit_scope=["src"],
    )
    # Only the in-scope-AND-in-diff path's secret surfaces.
    assert not result.passed
    assert "src/foo.py" in result.details
    assert "docs/bar.py" not in result.details


@pytest.mark.asyncio
async def test_secretscan_diff_paths_filtered_to_zero_returns_passed(
    tmp_path: Path,
) -> None:
    """When the intersection of ``paths`` and ``edit_scope`` is empty,
    nothing is scanned → trivially passes (even if both files contain
    secrets)."""
    (tmp_path / "src").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "src" / "foo.py").write_text("clean = True\n")
    (tmp_path / "docs" / "bar.py").write_text(
        "KEY = 'AKIAIOSFODNN7EXAMPLE'\n"
    )

    # paths is docs only, scope is src only → intersection empty.
    result = await run_secretscan(
        tmp_path,
        paths=[Path("docs/bar.py")],
        edit_scope=["src"],
    )
    assert result.passed
