"""Tests for v0.19.0 LLM equivalence judge (Stage 2)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from qa.llm_equivalence_judge import (
    LLMEquivalenceJudge,
    _cache_key,
    _load_cache,
    _parse_response,
    _append_cache,
)


def test_cache_key_is_deterministic() -> None:
    a = _cache_key("x = 1", "x = 2")
    b = _cache_key("x = 1", "x = 2")
    assert a == b
    assert _cache_key("x = 1", "x = 3") != a


def test_parse_response_yes_with_confidence() -> None:
    verdict, conf = _parse_response("YES\n0.92")
    assert verdict is True
    assert conf == pytest.approx(0.92)


def test_parse_response_no_with_confidence() -> None:
    verdict, conf = _parse_response("NO\n0.85")
    assert verdict is False
    assert conf == pytest.approx(0.85)


def test_parse_response_clamps_confidence() -> None:
    verdict, conf = _parse_response("YES\n2.5")
    assert conf == 1.0
    verdict, conf = _parse_response("NO\n-0.3")
    assert conf == 0.0


def test_parse_response_missing_confidence_defaults_zero() -> None:
    verdict, conf = _parse_response("YES")
    assert verdict is True
    assert conf == 0.0


def test_parse_response_empty_returns_no() -> None:
    assert _parse_response("") == (False, 0.0)


@pytest.mark.asyncio
async def test_identical_inputs_return_yes_without_api(tmp_path: Path) -> None:
    judge = LLMEquivalenceJudge(tmp_path)
    verdict, conf = await judge.is_equivalent("x = 1", "x = 1")
    assert verdict is True
    assert conf == 1.0


@pytest.mark.asyncio
async def test_no_client_returns_false_zero(tmp_path: Path) -> None:
    """When SDK unavailable / no API key, judge returns (False, 0.0)."""
    judge = LLMEquivalenceJudge(tmp_path)
    judge._client = None  # force no-client path
    verdict, conf = await judge.is_equivalent("x = 1", "x = 2")
    assert verdict is False
    assert conf == 0.0


@pytest.mark.asyncio
async def test_cache_hit_bypasses_api(tmp_path: Path) -> None:
    """A previously-recorded result short-circuits the API call."""
    key = _cache_key("a + 0", "a")
    _append_cache(tmp_path, key, True, 0.95)

    judge = LLMEquivalenceJudge(tmp_path)
    judge._client = AsyncMock()  # would-explode if called
    judge._client.messages.create = AsyncMock(side_effect=AssertionError("called"))

    verdict, conf = await judge.is_equivalent("a + 0", "a")
    assert verdict is True
    assert conf == 0.95


@pytest.mark.asyncio
async def test_api_failure_returns_false_zero(tmp_path: Path) -> None:
    judge = LLMEquivalenceJudge(tmp_path)
    judge._client = AsyncMock()
    judge._client.messages.create = AsyncMock(side_effect=RuntimeError("boom"))

    verdict, conf = await judge.is_equivalent("x = 1", "x = 2")
    assert verdict is False
    assert conf == 0.0


def test_load_cache_missing_returns_empty(tmp_path: Path) -> None:
    assert _load_cache(tmp_path) == {}


def test_load_cache_skips_malformed_lines(tmp_path: Path) -> None:
    autodev = tmp_path / ".autodev"
    autodev.mkdir()
    (autodev / "mutation_cache.jsonl").write_text(
        "garbage line\n"
        + json.dumps({"key": "abc", "verdict": True, "confidence": 0.9})
        + "\n"
        "{not json}\n"
    )
    cache = _load_cache(tmp_path)
    assert "abc" in cache


def test_append_cache_round_trip(tmp_path: Path) -> None:
    _append_cache(tmp_path, "k1", True, 0.9)
    _append_cache(tmp_path, "k2", False, 0.4)
    cache = _load_cache(tmp_path)
    assert cache["k1"]["verdict"] is True
    assert cache["k2"]["confidence"] == 0.4
