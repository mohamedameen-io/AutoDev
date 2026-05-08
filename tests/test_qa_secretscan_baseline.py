"""Tests for v0.19.0 secretscan baseline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qa.secretscan_baseline import (
    compute_baseline,
    filter_against_baseline,
    load_baseline,
)


@pytest.mark.asyncio
async def test_compute_baseline_writes_sidecar(tmp_path: Path) -> None:
    """Full-tree scan persists the keys to .autodev/secretscan-baseline.json."""
    secret = "AKIAABCDEFGHIJKLMNOP"
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "leaked.py").write_text(f'KEY = "{secret}"\n')

    keys = await compute_baseline(tmp_path)
    assert any("AWS access key" in k for k in keys)

    sidecar = tmp_path / ".autodev" / "secretscan-baseline.json"
    assert sidecar.exists()
    raw = json.loads(sidecar.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert any("AWS access key" in k for k in raw["keys"])


@pytest.mark.asyncio
async def test_filter_against_baseline_drops_known_findings(
    tmp_path: Path,
) -> None:
    """A finding already in the baseline is filtered out."""
    secret = "AKIAABCDEFGHIJKLMNOP"
    (tmp_path / "leaked.py").write_text(f'KEY = "{secret}"\n')
    await compute_baseline(tmp_path)

    findings = ["leaked.py: AWS access key"]
    out = await filter_against_baseline(findings, tmp_path)
    assert out == []


@pytest.mark.asyncio
async def test_filter_keeps_net_new_findings(tmp_path: Path) -> None:
    """Findings absent from baseline survive the filter."""
    secret = "AKIAABCDEFGHIJKLMNOP"
    (tmp_path / "leaked.py").write_text(f'KEY = "{secret}"\n')
    await compute_baseline(tmp_path)

    findings = [
        "leaked.py: AWS access key",  # in baseline
        "newfile.py: AWS access key",  # net-new
    ]
    out = await filter_against_baseline(findings, tmp_path)
    assert "newfile.py: AWS access key" in out
    assert "leaked.py: AWS access key" not in out


def test_load_baseline_missing_returns_empty(tmp_path: Path) -> None:
    """No baseline file → empty set, no error."""
    assert load_baseline(tmp_path) == set()


@pytest.mark.asyncio
async def test_filter_with_no_baseline_returns_input_unchanged(
    tmp_path: Path,
) -> None:
    """Missing baseline does NOT silently drop findings."""
    findings = ["a.py: AWS access key"]
    out = await filter_against_baseline(findings, tmp_path)
    assert out == findings


@pytest.mark.asyncio
async def test_compute_baseline_clean_repo(tmp_path: Path) -> None:
    """A clean repo writes an empty key set without error."""
    (tmp_path / "main.py").write_text("print('hello')\n")
    keys = await compute_baseline(tmp_path)
    assert keys == set()
    sidecar = tmp_path / ".autodev" / "secretscan-baseline.json"
    assert sidecar.exists()
    raw = json.loads(sidecar.read_text(encoding="utf-8"))
    assert raw["keys"] == []
