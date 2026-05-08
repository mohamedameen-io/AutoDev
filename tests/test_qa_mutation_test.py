"""Tests for v0.19.0 mutation-test gate (mutmut wrapper)."""

from __future__ import annotations

from pathlib import Path

import pytest

from qa import mutation_test as mt


@pytest.mark.asyncio
async def test_mutation_test_skip_when_mutmut_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No mutmut on PATH → pass with skip-and-warn detail."""
    monkeypatch.setattr(mt, "_mutmut_available", lambda: False)
    result = await mt.run_mutation_test(tmp_path)
    assert result.passed
    assert "skip-and-warn" in result.details


@pytest.mark.asyncio
async def test_mutation_test_kill_rate_below_threshold_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mt, "_mutmut_available", lambda: True)

    async def fake_invoke(cwd, paths, timeout_s):
        return 1, "", ""

    async def fake_results(cwd):
        return {"killed": 5, "survived": 5, "timeout": 0, "suspicious": 0}

    monkeypatch.setattr(mt, "_invoke_mutmut", fake_invoke)
    monkeypatch.setattr(mt, "_mutmut_results", fake_results)

    result = await mt.run_mutation_test(
        tmp_path, kill_rate_threshold=0.7
    )
    assert not result.passed
    assert "kill_rate=50.00%" in result.details


@pytest.mark.asyncio
async def test_mutation_test_kill_rate_above_threshold_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mt, "_mutmut_available", lambda: True)

    async def fake_invoke(cwd, paths, timeout_s):
        return 0, "", ""

    async def fake_results(cwd):
        return {"killed": 8, "survived": 2, "timeout": 0, "suspicious": 0}

    monkeypatch.setattr(mt, "_invoke_mutmut", fake_invoke)
    monkeypatch.setattr(mt, "_mutmut_results", fake_results)

    result = await mt.run_mutation_test(tmp_path, kill_rate_threshold=0.7)
    assert result.passed
    assert "kill_rate=80.00%" in result.details


@pytest.mark.asyncio
async def test_mutation_test_diff_scope_filters_python_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mt, "_mutmut_available", lambda: True)
    captured: dict = {}

    async def fake_invoke(cwd, paths, timeout_s):
        captured["paths"] = paths
        return 0, "", ""

    async def fake_results(cwd):
        return {"killed": 1, "survived": 0, "timeout": 0, "suspicious": 0}

    monkeypatch.setattr(mt, "_invoke_mutmut", fake_invoke)
    monkeypatch.setattr(mt, "_mutmut_results", fake_results)

    paths = [Path("a.py"), Path("b.txt"), Path("c.py")]
    result = await mt.run_mutation_test(tmp_path, paths=paths)
    assert result.passed
    assert captured["paths"] == [Path("a.py"), Path("c.py")]


@pytest.mark.asyncio
async def test_mutation_test_no_python_in_diff_scope_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mt, "_mutmut_available", lambda: True)
    paths = [Path("a.txt"), Path("b.md")]
    result = await mt.run_mutation_test(tmp_path, paths=paths)
    assert result.passed
    assert "no Python files in diff scope" in result.details


@pytest.mark.asyncio
async def test_mutation_test_timeout_skip_and_warn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mt, "_mutmut_available", lambda: True)

    async def fake_invoke(cwd, paths, timeout_s):
        return 124, "", "<timeout>"

    monkeypatch.setattr(mt, "_invoke_mutmut", fake_invoke)
    result = await mt.run_mutation_test(tmp_path, timeout_s=10)
    assert result.passed
    assert "timeout" in result.details


@pytest.mark.asyncio
async def test_mutation_test_unparseable_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mt, "_mutmut_available", lambda: True)

    async def fake_invoke(cwd, paths, timeout_s):
        return 0, "", ""

    async def fake_results(cwd):
        return {}

    monkeypatch.setattr(mt, "_invoke_mutmut", fake_invoke)
    monkeypatch.setattr(mt, "_mutmut_results", fake_results)

    result = await mt.run_mutation_test(tmp_path)
    assert result.passed
    assert "no parseable results" in result.details


def test_kill_rate_zero_total_returns_one() -> None:
    """When no mutants were attempted, kill_rate=1.0 (vacuously sufficient)."""
    rate = mt._kill_rate({})
    assert rate == 1.0


def test_kill_rate_excludes_skipped_from_denominator() -> None:
    rate = mt._kill_rate(
        {"killed": 8, "survived": 2, "timeout": 0, "suspicious": 0, "skipped": 100}
    )
    assert rate == 0.8


def test_kill_rate_treats_suspicious_as_unkilled() -> None:
    rate = mt._kill_rate(
        {"killed": 5, "survived": 0, "timeout": 0, "suspicious": 5}
    )
    assert rate == 0.5
