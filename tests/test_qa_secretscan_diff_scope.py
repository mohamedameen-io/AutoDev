"""Tests for v0.13.0 ``run_secretscan(paths=...)`` diff-scope filter.

The legacy v0.12.0 contract — ``run_secretscan(cwd) -> GateResult`` — walks
the entire ``cwd`` tree. v0.13.0 extends the signature with an optional
``paths`` parameter:

* ``paths=None`` (default) → legacy full-walk behavior, regression-tested
  here so any future refactor doesn't break the contract.
* ``paths=[...]`` → scan only the specified files (resolved relative to
  ``cwd``). Non-existent paths are silently skipped.

Motivation: the Unity QNX run hit 28k pre-existing secret findings from
.git/objects, vendored deps, and historical config. Restricting the scan
to the developer's diff means the gate only blocks on net-new secrets the
LLM just introduced — pre-existing repo state is no longer the executor's
concern.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qa.secretscan import run_secretscan


@pytest.mark.asyncio
async def test_run_secretscan_paths_none_walks_full_cwd(tmp_path: Path) -> None:
    """Regression: ``paths=None`` (default) preserves the legacy walk.

    A secret anywhere in cwd is still found.
    """
    (tmp_path / "config.py").write_text("AWS_KEY = 'AKIAIOSFODNN7EXAMPLE'\n")
    result = await run_secretscan(tmp_path)
    assert not result.passed
    assert "AWS" in result.details


@pytest.mark.asyncio
async def test_run_secretscan_paths_empty_list_returns_passed(
    tmp_path: Path,
) -> None:
    """An empty paths list scans nothing → trivially passes.

    This is the "developer made no changes" boundary: skip the gate.
    """
    # Even with a secret on disk, an empty paths filter ignores it.
    (tmp_path / "config.py").write_text("AWS_KEY = 'AKIAIOSFODNN7EXAMPLE'\n")
    result = await run_secretscan(tmp_path, paths=[])
    assert result.passed


@pytest.mark.asyncio
async def test_run_secretscan_paths_filters_to_specified_files(
    tmp_path: Path,
) -> None:
    """Only the listed files are scanned. Pre-existing secrets in untouched
    files are NOT reported.

    file_a contains a secret; file_b is benign. Scan only file_b → pass.
    """
    (tmp_path / "file_a.py").write_text("KEY = 'AKIAIOSFODNN7EXAMPLE'\n")
    (tmp_path / "file_b.py").write_text("def hello():\n    return 'world'\n")

    result = await run_secretscan(tmp_path, paths=[Path("file_b.py")])
    assert result.passed


@pytest.mark.asyncio
async def test_run_secretscan_paths_finds_secret_in_filtered_file(
    tmp_path: Path,
) -> None:
    """When the filtered file *does* contain a secret, the gate fails."""
    (tmp_path / "secret_file.py").write_text("KEY = 'AKIAIOSFODNN7EXAMPLE'\n")
    (tmp_path / "clean_file.py").write_text("def f(): return 1\n")

    result = await run_secretscan(tmp_path, paths=[Path("secret_file.py")])
    assert not result.passed
    assert "AWS" in result.details


@pytest.mark.asyncio
async def test_run_secretscan_paths_skips_nonexistent_paths(
    tmp_path: Path,
) -> None:
    """Paths that don't exist (e.g. file deleted between diff capture and
    gate run) are silently ignored — no errors raised."""
    (tmp_path / "real.py").write_text("def f(): return 1\n")

    result = await run_secretscan(
        tmp_path,
        paths=[Path("real.py"), Path("ghost.py"), Path("nope/missing.py")],
    )
    assert result.passed


@pytest.mark.asyncio
async def test_run_secretscan_paths_resolves_relative_to_cwd(
    tmp_path: Path,
) -> None:
    """Paths in the list are resolved relative to ``cwd`` even when given
    as bare strings / non-anchored Paths."""
    nested = tmp_path / "src" / "deep"
    nested.mkdir(parents=True)
    (nested / "secret.py").write_text("PAT = 'ghp_abcdefghijklmnopqrstuvwxyz1234567890'\n")

    result = await run_secretscan(tmp_path, paths=[Path("src/deep/secret.py")])
    assert not result.passed
    assert "GitHub" in result.details


@pytest.mark.asyncio
async def test_run_secretscan_paths_handles_absolute_paths(
    tmp_path: Path,
) -> None:
    """Absolute paths under ``cwd`` are also accepted (the function will
    .relative_to(cwd) for reporting)."""
    target = tmp_path / "abs.py"
    target.write_text("KEY = 'AKIAIOSFODNN7EXAMPLE'\n")
    result = await run_secretscan(tmp_path, paths=[target])
    assert not result.passed


@pytest.mark.asyncio
async def test_run_secretscan_paths_skips_skip_extensions(
    tmp_path: Path,
) -> None:
    """Even when explicitly listed, ``.pyc`` / ``.png`` etc. are skipped
    (legacy SKIP_EXTENSIONS contract preserved on the diff-scope path).
    """
    (tmp_path / "compiled.pyc").write_bytes(b"AKIAIOSFODNN7EXAMPLE")
    result = await run_secretscan(tmp_path, paths=[Path("compiled.pyc")])
    assert result.passed
