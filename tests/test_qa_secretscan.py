"""Tests for :mod:`src.qa.secretscan`."""

from __future__ import annotations

from pathlib import Path

import pytest

from qa.secretscan import run_secretscan


@pytest.mark.asyncio
async def test_secretscan_clean_project(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def hello():\n    return 'world'\n")
    result = await run_secretscan(tmp_path)
    assert result.passed
    assert "no secrets" in result.details


@pytest.mark.asyncio
async def test_secretscan_detects_aws_key(tmp_path: Path) -> None:
    (tmp_path / "config.py").write_text("AWS_KEY = 'AKIAIOSFODNN7EXAMPLE'\n")
    result = await run_secretscan(tmp_path)
    assert not result.passed
    assert "AWS" in result.details


@pytest.mark.asyncio
async def test_secretscan_detects_github_pat(tmp_path: Path) -> None:
    (tmp_path / "deploy.sh").write_text("TOKEN=ghp_abcdefghijklmnopqrstuvwxyz1234567890\n")
    result = await run_secretscan(tmp_path)
    assert not result.passed
    assert "GitHub" in result.details


@pytest.mark.asyncio
async def test_secretscan_detects_private_key(tmp_path: Path) -> None:
    (tmp_path / "key.pem").write_text("-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----\n")
    result = await run_secretscan(tmp_path)
    assert not result.passed
    assert "Private key" in result.details


@pytest.mark.asyncio
async def test_secretscan_detects_high_entropy(tmp_path: Path) -> None:
    # A high-entropy base64-like string in a quoted assignment.
    (tmp_path / "settings.py").write_text('SECRET_KEY = "aB3dEfGhIjKlMnOpQrStUvWxYz012345"\n')
    result = await run_secretscan(tmp_path)
    # High entropy string should be flagged.
    assert not result.passed


@pytest.mark.asyncio
async def test_secretscan_skips_venv(tmp_path: Path) -> None:
    venv_dir = tmp_path / ".venv" / "lib"
    venv_dir.mkdir(parents=True)
    (venv_dir / "secret.py").write_text("KEY = 'AKIAIOSFODNN7EXAMPLE'\n")
    result = await run_secretscan(tmp_path)
    assert result.passed


@pytest.mark.asyncio
async def test_secretscan_skips_pyc(tmp_path: Path) -> None:
    (tmp_path / "compiled.pyc").write_bytes(b"AKIAIOSFODNN7EXAMPLE")
    result = await run_secretscan(tmp_path)
    assert result.passed


@pytest.mark.asyncio
async def test_secretscan_empty_dir(tmp_path: Path) -> None:
    result = await run_secretscan(tmp_path)
    assert result.passed
