"""Tests for v0.19.0 .autodev/secretscan-allow allowlist (gitignore-style globs)."""

from __future__ import annotations

from pathlib import Path

import pytest

from qa.secretscan import run_secretscan


@pytest.mark.asyncio
async def test_allowlist_skips_matching_files(tmp_path: Path) -> None:
    """Files matching a .autodev/secretscan-allow glob are skipped."""
    secret = "AKIAABCDEFGHIJKLMNOP"
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "test_secrets.py").write_text(
        f'KEY = "{secret}"\n'
    )
    autodev = tmp_path / ".autodev"
    autodev.mkdir()
    (autodev / "secretscan-allow").write_text("fixtures/*.py\n")

    result = await run_secretscan(tmp_path)
    assert result.passed, result.details


@pytest.mark.asyncio
async def test_allowlist_does_not_affect_unmatched_files(tmp_path: Path) -> None:
    """Files outside allowlist still get scanned."""
    secret = "AKIAABCDEFGHIJKLMNOP"
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "leaked.py").write_text(f'KEY = "{secret}"\n')
    autodev = tmp_path / ".autodev"
    autodev.mkdir()
    (autodev / "secretscan-allow").write_text("fixtures/*.py\n")

    result = await run_secretscan(tmp_path)
    assert not result.passed
    assert "AWS access key" in result.details


@pytest.mark.asyncio
async def test_allowlist_missing_file_is_no_op(tmp_path: Path) -> None:
    """No allowlist file → behaves as before."""
    secret = "AKIAABCDEFGHIJKLMNOP"
    (tmp_path / "leaked.py").write_text(f'KEY = "{secret}"\n')
    result = await run_secretscan(tmp_path)
    assert not result.passed


@pytest.mark.asyncio
async def test_allowlist_supports_directory_glob(tmp_path: Path) -> None:
    """Directory globs (``fixtures/**``) skip an entire subtree."""
    secret = "AKIAABCDEFGHIJKLMNOP"
    (tmp_path / "fixtures" / "nested").mkdir(parents=True)
    (tmp_path / "fixtures" / "nested" / "leaked.py").write_text(
        f'KEY = "{secret}"\n'
    )
    autodev = tmp_path / ".autodev"
    autodev.mkdir()
    (autodev / "secretscan-allow").write_text("fixtures/**\n")

    result = await run_secretscan(tmp_path)
    assert result.passed, result.details


@pytest.mark.asyncio
async def test_allowlist_ignores_blank_and_comment_lines(tmp_path: Path) -> None:
    """Blank lines and ``#``-prefixed comments are ignored."""
    secret = "AKIAABCDEFGHIJKLMNOP"
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "test_secrets.py").write_text(
        f'KEY = "{secret}"\n'
    )
    autodev = tmp_path / ".autodev"
    autodev.mkdir()
    (autodev / "secretscan-allow").write_text(
        "# top comment\n\nfixtures/*.py\n# trailing\n"
    )

    result = await run_secretscan(tmp_path)
    assert result.passed, result.details
