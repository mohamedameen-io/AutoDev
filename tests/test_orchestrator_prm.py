"""v0.15.0: PRM trajectory pattern detection.

Pure unit tests for :mod:`orchestrator.prm`. The module records
:class:`TrajectoryEvent` for every delegate dispatch and runs five
rule-based pattern detectors against the trailing event window:

* ``repetition_loop`` — same ``(role, action, target_files)`` triple
  observed ≥3× in a row.
* ``ping_pong`` — alternating between two distinct targets ≥4×.
* ``expansion_drift`` — target_files set growing without success.
* ``stuck_on_test`` — test-engineer role with ≥3 consecutive failures.
* ``context_thrash`` — rapid switching between unrelated targets.

Pattern → taxonomy mapping (see plan section "PRM trajectory pattern
detection"):

* ``repetition_loop, ping_pong``       → ``"reasoning_error"``
* ``expansion_drift``                  → ``"specification_error"``
* ``stuck_on_test, context_thrash``    → ``"coordination_error"``

The :class:`CourseCorrection` data class carries the taxonomy + pattern
+ suggestion and renders to a markdown block via :meth:`format_for_prompt`.
"""

from __future__ import annotations

import pytest

from orchestrator.prm import (
    CourseCorrection,
    Pattern,
    TrajectoryEvent,
    TrajectoryStore,
    detect_context_thrash,
    detect_expansion_drift,
    detect_ping_pong,
    detect_repetition_loop,
    detect_stuck_on_test,
)


def _ev(
    role: str = "developer",
    action: str = "edit",
    target_files: tuple[str, ...] = ("src/foo.py",),
    success: bool = False,
    duration_s: float = 1.0,
    timestamp: float = 0.0,
) -> TrajectoryEvent:
    return TrajectoryEvent(
        timestamp=timestamp,
        role=role,
        action=action,
        target_files=target_files,
        success=success,
        duration_s=duration_s,
    )


# ---------------------------------------------------------------------------
# detect_repetition_loop
# ---------------------------------------------------------------------------


def test_repetition_loop_detected_after_3_identical_events() -> None:
    events = [_ev() for _ in range(3)]
    pattern = detect_repetition_loop(events)
    assert pattern is not None
    assert pattern.name == "repetition_loop"


def test_repetition_loop_NOT_detected_on_2_identical_events() -> None:
    events = [_ev() for _ in range(2)]
    assert detect_repetition_loop(events) is None


def test_repetition_loop_requires_consecutive_match() -> None:
    """Three identical events broken by a different event in the middle
    must NOT count as a repetition loop."""
    events = [
        _ev(target_files=("a.py",)),
        _ev(target_files=("b.py",)),
        _ev(target_files=("a.py",)),
        _ev(target_files=("c.py",)),
        _ev(target_files=("a.py",)),
    ]
    assert detect_repetition_loop(events) is None


# ---------------------------------------------------------------------------
# detect_ping_pong
# ---------------------------------------------------------------------------


def test_ping_pong_detected_after_4_alternations() -> None:
    events = [
        _ev(target_files=("a.py",)),
        _ev(target_files=("b.py",)),
        _ev(target_files=("a.py",)),
        _ev(target_files=("b.py",)),
    ]
    pattern = detect_ping_pong(events)
    assert pattern is not None
    assert pattern.name == "ping_pong"


def test_ping_pong_requires_at_least_4_events() -> None:
    """Three events alternating only ABA — does NOT yet meet threshold."""
    events = [
        _ev(target_files=("a.py",)),
        _ev(target_files=("b.py",)),
        _ev(target_files=("a.py",)),
    ]
    assert detect_ping_pong(events) is None


def test_ping_pong_NOT_detected_on_3_distinct_targets() -> None:
    """A→B→C→D is NOT ping-pong (more than 2 unique targets)."""
    events = [
        _ev(target_files=("a.py",)),
        _ev(target_files=("b.py",)),
        _ev(target_files=("c.py",)),
        _ev(target_files=("d.py",)),
    ]
    assert detect_ping_pong(events) is None


# ---------------------------------------------------------------------------
# detect_expansion_drift
# ---------------------------------------------------------------------------


def test_expansion_drift_detected_when_target_set_grows() -> None:
    """The trailing window's target_files monotonically grows with no success."""
    events = [
        _ev(target_files=("a.py",), success=False),
        _ev(target_files=("a.py", "b.py"), success=False),
        _ev(target_files=("a.py", "b.py", "c.py"), success=False),
    ]
    pattern = detect_expansion_drift(events)
    assert pattern is not None
    assert pattern.name == "expansion_drift"


def test_expansion_drift_NOT_detected_when_success_present() -> None:
    """A successful event in the window resets the drift signal."""
    events = [
        _ev(target_files=("a.py",), success=True),
        _ev(target_files=("a.py", "b.py"), success=False),
        _ev(target_files=("a.py", "b.py", "c.py"), success=False),
    ]
    assert detect_expansion_drift(events) is None


def test_expansion_drift_NOT_detected_on_shrinking_target_set() -> None:
    """A shrinking target set is NOT expansion drift."""
    events = [
        _ev(target_files=("a.py", "b.py", "c.py"), success=False),
        _ev(target_files=("a.py", "b.py"), success=False),
        _ev(target_files=("a.py",), success=False),
    ]
    assert detect_expansion_drift(events) is None


# ---------------------------------------------------------------------------
# detect_stuck_on_test
# ---------------------------------------------------------------------------


def test_stuck_on_test_detected_after_3_consecutive_test_engineer_failures() -> None:
    events = [
        _ev(role="test_engineer", action="test", success=False),
        _ev(role="test_engineer", action="test", success=False),
        _ev(role="test_engineer", action="test", success=False),
    ]
    pattern = detect_stuck_on_test(events)
    assert pattern is not None
    assert pattern.name == "stuck_on_test"


def test_stuck_on_test_NOT_detected_when_test_engineer_succeeds_once() -> None:
    events = [
        _ev(role="test_engineer", action="test", success=False),
        _ev(role="test_engineer", action="test", success=True),
        _ev(role="test_engineer", action="test", success=False),
    ]
    assert detect_stuck_on_test(events) is None


def test_stuck_on_test_NOT_detected_for_non_test_engineer_failures() -> None:
    """3 developer failures should NOT trip stuck_on_test (different role)."""
    events = [
        _ev(role="developer", success=False),
        _ev(role="developer", success=False),
        _ev(role="developer", success=False),
    ]
    assert detect_stuck_on_test(events) is None


# ---------------------------------------------------------------------------
# detect_context_thrash
# ---------------------------------------------------------------------------


def test_context_thrash_detected_on_unrelated_target_switching() -> None:
    """Five rapid switches between targets that share no files."""
    events = [
        _ev(target_files=("a.py",)),
        _ev(target_files=("docs/x.md",)),
        _ev(target_files=("tests/test_a.py",)),
        _ev(target_files=("scripts/y.sh",)),
        _ev(target_files=("config.toml",)),
    ]
    pattern = detect_context_thrash(events)
    assert pattern is not None
    assert pattern.name == "context_thrash"


def test_context_thrash_NOT_detected_on_related_targets() -> None:
    """Targets in the same directory share file overlap → not thrash."""
    events = [
        _ev(target_files=("src/a.py",)),
        _ev(target_files=("src/a.py", "src/b.py")),
        _ev(target_files=("src/b.py",)),
        _ev(target_files=("src/a.py",)),
    ]
    assert detect_context_thrash(events) is None


# ---------------------------------------------------------------------------
# Pattern → taxonomy mapping
# ---------------------------------------------------------------------------


def test_pattern_repetition_loop_maps_to_reasoning_error() -> None:
    p = Pattern(name="repetition_loop")
    assert p.taxonomy == "reasoning_error"


def test_pattern_ping_pong_maps_to_reasoning_error() -> None:
    p = Pattern(name="ping_pong")
    assert p.taxonomy == "reasoning_error"


def test_pattern_expansion_drift_maps_to_specification_error() -> None:
    p = Pattern(name="expansion_drift")
    assert p.taxonomy == "specification_error"


def test_pattern_stuck_on_test_maps_to_coordination_error() -> None:
    p = Pattern(name="stuck_on_test")
    assert p.taxonomy == "coordination_error"


def test_pattern_context_thrash_maps_to_coordination_error() -> None:
    p = Pattern(name="context_thrash")
    assert p.taxonomy == "coordination_error"


# ---------------------------------------------------------------------------
# TrajectoryStore behavior
# ---------------------------------------------------------------------------


def test_trajectory_store_capped_at_50_events() -> None:
    store = TrajectoryStore()
    for i in range(60):
        store.record("task-1", _ev(timestamp=float(i)))
    events = store.events_for("task-1")
    assert len(events) == 50
    # Oldest events evicted; newest preserved.
    assert events[-1].timestamp == 59.0
    assert events[0].timestamp == 10.0


def test_trajectory_store_isolates_per_task() -> None:
    store = TrajectoryStore()
    store.record("task-A", _ev())
    store.record("task-A", _ev())
    store.record("task-B", _ev())
    assert len(store.events_for("task-A")) == 2
    assert len(store.events_for("task-B")) == 1


def test_trajectory_store_analyze_returns_patterns() -> None:
    store = TrajectoryStore()
    for _ in range(3):
        store.record("task-1", _ev())
    patterns = store.analyze("task-1")
    assert any(p.name == "repetition_loop" for p in patterns)


def test_trajectory_store_analyze_empty_returns_empty_list() -> None:
    store = TrajectoryStore()
    assert store.analyze("task-never-seen") == []


# ---------------------------------------------------------------------------
# CourseCorrection
# ---------------------------------------------------------------------------


def test_course_correction_format_includes_taxonomy_pattern_suggestion() -> None:
    cc = CourseCorrection(
        taxonomy="reasoning_error",
        pattern="repetition_loop",
        suggestion="vary the approach; you've made the same edit 3x",
    )
    out = cc.format_for_prompt()
    assert "## COURSE CORRECTION" in out
    assert "reasoning_error" in out
    assert "repetition_loop" in out
    assert "vary the approach" in out


def test_course_correction_default_suggestion_per_pattern() -> None:
    """A factory hook builds a CourseCorrection from a Pattern with a
    sensible default suggestion. This keeps the wiring layer (commit 13)
    independent of bespoke per-pattern suggestion text."""
    p = Pattern(name="repetition_loop")
    cc = CourseCorrection.from_pattern(p)
    assert cc.taxonomy == "reasoning_error"
    assert cc.pattern == "repetition_loop"
    assert cc.suggestion  # non-empty


@pytest.mark.parametrize(
    "name,taxonomy",
    [
        ("repetition_loop", "reasoning_error"),
        ("ping_pong", "reasoning_error"),
        ("expansion_drift", "specification_error"),
        ("stuck_on_test", "coordination_error"),
        ("context_thrash", "coordination_error"),
    ],
)
def test_pattern_to_taxonomy_table(name: str, taxonomy: str) -> None:
    assert Pattern(name=name).taxonomy == taxonomy
