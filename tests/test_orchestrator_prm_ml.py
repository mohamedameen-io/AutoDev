"""v0.20.0 A1: LLMTrajectoryClassifier + merge_patterns tests."""

from __future__ import annotations

import pytest

from orchestrator.prm import (
    LLMTrajectoryClassifier,
    Pattern,
    TrajectoryEvent,
    merge_patterns,
)


def _ev(role: str, action: str, files: tuple[str, ...] = (), success: bool = True) -> TrajectoryEvent:
    return TrajectoryEvent(
        timestamp=1.0,
        role=role,
        action=action,
        target_files=files,
        success=success,
        duration_s=0.5,
    )


@pytest.mark.asyncio
async def test_classifier_returns_patterns_above_threshold() -> None:
    """A stubbed completer returning JSON yields parsed Pattern objects."""

    async def stub_completer(prompt: str) -> str:
        return '{"patterns": [{"name": "stuck_on_test", "confidence": 0.85}]}'

    clf = LLMTrajectoryClassifier(completer=stub_completer, threshold=0.7)
    events = [_ev("test_engineer", "test", files=("a.py",)) for _ in range(3)]
    patterns = await clf.classify(events)
    assert len(patterns) == 1
    assert patterns[0].name == "stuck_on_test"


@pytest.mark.asyncio
async def test_classifier_drops_patterns_below_threshold() -> None:
    """Confidence below threshold filters the pattern out."""

    async def stub_completer(prompt: str) -> str:
        return '{"patterns": [{"name": "stuck_on_test", "confidence": 0.5}]}'

    clf = LLMTrajectoryClassifier(completer=stub_completer, threshold=0.7)
    events = [_ev("test_engineer", "test", files=("a.py",)) for _ in range(3)]
    patterns = await clf.classify(events)
    assert patterns == []


@pytest.mark.asyncio
async def test_classifier_drops_unknown_pattern_names() -> None:
    """LLM hallucinations of pattern names are silently dropped."""

    async def stub_completer(prompt: str) -> str:
        return '{"patterns": [{"name": "made_up_pattern", "confidence": 0.95}]}'

    clf = LLMTrajectoryClassifier(completer=stub_completer, threshold=0.7)
    events = [_ev("test_engineer", "test", files=("a.py",)) for _ in range(3)]
    patterns = await clf.classify(events)
    assert patterns == []


@pytest.mark.asyncio
async def test_classifier_gracefully_handles_completer_exception() -> None:
    """Any exception from the completer yields ``[]`` (no crash)."""

    async def boom(prompt: str) -> str:
        raise RuntimeError("network down")

    clf = LLMTrajectoryClassifier(completer=boom, threshold=0.7)
    events = [_ev("test_engineer", "test", files=("a.py",)) for _ in range(5)]
    patterns = await clf.classify(events)
    assert patterns == []


@pytest.mark.asyncio
async def test_classifier_gracefully_handles_malformed_response() -> None:
    """Non-JSON response yields ``[]`` via the regex fallback."""

    async def stub_completer(prompt: str) -> str:
        return "I am not JSON, sorry"

    clf = LLMTrajectoryClassifier(completer=stub_completer, threshold=0.7)
    events = [_ev("test_engineer", "test", files=("a.py",)) for _ in range(3)]
    patterns = await clf.classify(events)
    assert patterns == []


@pytest.mark.asyncio
async def test_classifier_skips_below_min_events() -> None:
    """Cold-start: <min_events skip the LLM entirely."""
    calls = []

    async def tracking_completer(prompt: str) -> str:
        calls.append(prompt)
        return '{"patterns": []}'

    clf = LLMTrajectoryClassifier(
        completer=tracking_completer, threshold=0.7, min_events=5
    )
    # Only 2 events — below min_events
    events = [_ev("developer", "edit", files=("a.py",)) for _ in range(2)]
    patterns = await clf.classify(events)
    assert patterns == []
    assert calls == []  # completer never invoked


@pytest.mark.asyncio
async def test_classifier_supports_regex_fallback_for_json_with_prose() -> None:
    """Trailing prose after the JSON block doesn't break parsing."""

    async def stub_completer(prompt: str) -> str:
        return (
            'Here is my analysis:\n'
            '{"patterns": [{"name": "stuck_on_test", "confidence": 0.9}]}\n'
            "Best, your friendly LLM."
        )

    clf = LLMTrajectoryClassifier(completer=stub_completer, threshold=0.7)
    events = [_ev("test_engineer", "test", files=("a.py",)) for _ in range(3)]
    patterns = await clf.classify(events)
    assert len(patterns) == 1
    assert patterns[0].name == "stuck_on_test"


def test_merge_patterns_dedupes_rules_priority() -> None:
    """When the same pattern appears in both lists, rules win."""
    rules = [Pattern(name="stuck_on_test")]
    ml = [Pattern(name="stuck_on_test"), Pattern(name="repetition_loop")]
    merged = merge_patterns(rules, ml)
    names = [p.name for p in merged]
    assert names.count("stuck_on_test") == 1
    assert "repetition_loop" in names


def test_merge_patterns_sorts_by_severity_descending() -> None:
    """Merged list is sorted highest-severity-first."""
    rules = [Pattern(name="repetition_loop")]  # severity 1
    ml = [Pattern(name="stuck_on_test")]  # severity 5
    merged = merge_patterns(rules, ml)
    assert merged[0].name == "stuck_on_test"
    assert merged[-1].name == "repetition_loop"


def test_merge_patterns_empty_inputs() -> None:
    assert merge_patterns([], []) == []
    p = Pattern(name="ping_pong")
    assert merge_patterns([p], []) == [p]
    assert merge_patterns([], [p]) == [p]


@pytest.mark.asyncio
async def test_classifier_threshold_clamped_to_unit_interval() -> None:
    """Threshold values outside [0, 1] are clamped."""

    async def stub_completer(prompt: str) -> str:
        return '{"patterns": []}'

    clf_high = LLMTrajectoryClassifier(completer=stub_completer, threshold=2.0)
    clf_low = LLMTrajectoryClassifier(completer=stub_completer, threshold=-0.5)
    assert clf_high.threshold == 1.0
    assert clf_low.threshold == 0.0


def test_prm_config_defaults_preserve_legacy_behavior() -> None:
    from config.schema import PRMConfig

    cfg = PRMConfig()
    assert cfg.strategy == "rules"
    assert 0.0 <= cfg.ml_threshold <= 1.0
    assert cfg.ml_min_events >= 1
