"""Tests for :func:`orchestrator.corrective_parser.parse_corrective_direction`."""

from __future__ import annotations

import pytest

from orchestrator.corrective_parser import parse_corrective_direction


def test_simple_bullet_list_yields_one_task_per_bullet() -> None:
    text = (
        "- Fix the macOS dispatcher flake\n"
        "- Add a regression test for the queue\n"
    )
    tasks = parse_corrective_direction(
        text, phase_id="1", base_task_count=3
    )
    assert len(tasks) == 2
    assert tasks[0].title == "Fix the macOS dispatcher flake"
    assert tasks[1].title == "Add a regression test for the queue"


def test_multi_line_bullet_preserves_description_full_body() -> None:
    """A bullet's title is the first line; the full body is the
    description (so indented sub-detail isn't lost)."""
    text = (
        "- Fix the macOS dispatcher flake\n"
        "  The hang shows up in worker.py — investigate the\n"
        "  asyncio.wait_for path and add a backoff.\n"
    )
    tasks = parse_corrective_direction(
        text, phase_id="1", base_task_count=0
    )
    assert len(tasks) == 1
    assert tasks[0].title == "Fix the macOS dispatcher flake"
    assert "asyncio.wait_for" in tasks[0].description
    assert "Fix the macOS dispatcher flake" in tasks[0].description


def test_phase_id_threading_correct() -> None:
    """``phase_id`` ends up on each Task and prefixes the generated id."""
    text = "- One correction\n- Another correction\n"
    tasks = parse_corrective_direction(
        text, phase_id="2", base_task_count=4
    )
    assert tasks[0].phase_id == "2"
    assert tasks[0].id == "2.c5"
    assert tasks[1].phase_id == "2"
    assert tasks[1].id == "2.c6"


def test_complexity_inheritance_from_phase_complexity_arg() -> None:
    """``phase_complexity="complex"`` is stamped on all corrective tasks."""
    text = "- Investigate root cause\n"
    tasks = parse_corrective_direction(
        text, phase_id="1", base_task_count=0, phase_complexity="complex"
    )
    assert tasks[0].complexity == "complex"


def test_complexity_falls_back_to_medium_when_none() -> None:
    """When ``phase_complexity`` is ``None`` the parser stamps ``"medium"``
    (matching the orchestrator's spec-fallback shape)."""
    text = "- Add coverage\n"
    tasks = parse_corrective_direction(
        text, phase_id="1", base_task_count=0, phase_complexity=None
    )
    assert tasks[0].complexity == "medium"


def test_task_id_increments_from_base_task_count() -> None:
    text = "- one\n- two\n- three\n"
    tasks = parse_corrective_direction(
        text, phase_id="3", base_task_count=10
    )
    assert [t.id for t in tasks] == ["3.c11", "3.c12", "3.c13"]


def test_metadata_includes_origin_and_tournament_id() -> None:
    text = "- corrective bullet\n"
    tasks = parse_corrective_direction(
        text,
        phase_id="1",
        base_task_count=0,
        tournament_id="phase-review-deadbeef-1",
    )
    assert tasks[0].metadata["origin"] == "phase_review_corrective"
    assert tasks[0].metadata["tournament_id"] == "phase-review-deadbeef-1"


def test_empty_direction_returns_empty_list() -> None:
    assert parse_corrective_direction("", phase_id="1", base_task_count=0) == []
    assert (
        parse_corrective_direction(
            "   \n  \n", phase_id="1", base_task_count=0
        )
        == []
    )


def test_malformed_direction_no_bullets_returns_empty_list() -> None:
    """Free-form prose with no bullet markers → no tasks."""
    text = "We should fix the dispatcher. Also add tests."
    assert (
        parse_corrective_direction(text, phase_id="1", base_task_count=0) == []
    )


def test_developer_assigned_by_default() -> None:
    """Corrective tasks always run via the developer agent."""
    text = "- thing\n"
    tasks = parse_corrective_direction(
        text, phase_id="1", base_task_count=0
    )
    assert tasks[0].assigned_agent == "developer"


def test_numeric_bullets_recognized() -> None:
    """``1.`` / ``2.`` style bullets are recognized as top-level."""
    text = "1. first thing\n2. second thing\n"
    tasks = parse_corrective_direction(
        text, phase_id="1", base_task_count=0
    )
    assert len(tasks) == 2
    assert tasks[0].title == "first thing"
    assert tasks[1].title == "second thing"


def test_nested_bullets_stay_within_parent_description() -> None:
    """Indented sub-bullets do NOT split into separate top-level tasks."""
    text = (
        "- Top-level fix\n"
        "  - sub-bullet 1\n"
        "  - sub-bullet 2\n"
        "- Another top-level fix\n"
    )
    tasks = parse_corrective_direction(
        text, phase_id="1", base_task_count=0
    )
    assert len(tasks) == 2
    assert "sub-bullet 1" in tasks[0].description
    assert "sub-bullet 2" in tasks[0].description


def test_parse_corrective_direction_caps_at_max_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.37.0 H2: ``max_tasks=N`` truncates to N tasks and surfaces the
    dropped count via the ``corrective_parser.parsed`` log event.

    Pinned because plan inflation observed in real-world runs can emit
    long bullet lists in a single round; without this cap a single
    architect-refine response could exhaust the phase's lifetime budget.
    """
    from orchestrator import corrective_parser

    captured: list[dict] = []

    class _CaptureLogger:
        def info(self, event: str, **kwargs) -> None:  # noqa: ANN003
            captured.append({"event": event, **kwargs})

        def warning(self, event: str, **kwargs) -> None:  # noqa: ANN003
            captured.append({"event": event, "level": "warning", **kwargs})

    monkeypatch.setattr(corrective_parser, "logger", _CaptureLogger())

    text = (
        "- one\n"
        "- two\n"
        "- three\n"
        "- four\n"
        "- five\n"
        "- six\n"
        "- seven\n"
    )
    tasks = parse_corrective_direction(
        text,
        phase_id="1",
        base_task_count=0,
        max_tasks=3,
    )
    assert len(tasks) == 3
    assert [t.title for t in tasks] == ["one", "two", "three"]

    parsed = [c for c in captured if c["event"] == "corrective_parser.parsed"]
    assert len(parsed) == 1
    assert parsed[0]["produced"] == 3
    assert parsed[0]["dropped"] == 4
    assert parsed[0]["max_tasks"] == 3


def test_parse_corrective_direction_no_cap_preserves_legacy_behaviour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``max_tasks=None`` (default) emits ``dropped=0`` and keeps all
    bullets, matching pre-v0.37.0 behaviour for callers that have not
    opted into the cap path."""
    from orchestrator import corrective_parser

    captured: list[dict] = []

    class _CaptureLogger:
        def info(self, event: str, **kwargs) -> None:  # noqa: ANN003
            captured.append({"event": event, **kwargs})

        def warning(self, event: str, **kwargs) -> None:  # noqa: ANN003
            captured.append({"event": event, "level": "warning", **kwargs})

    monkeypatch.setattr(corrective_parser, "logger", _CaptureLogger())

    text = "- one\n- two\n- three\n"
    tasks = parse_corrective_direction(
        text, phase_id="1", base_task_count=0
    )
    assert len(tasks) == 3
    parsed = [c for c in captured if c["event"] == "corrective_parser.parsed"]
    assert parsed[0]["dropped"] == 0
    assert parsed[0]["max_tasks"] is None
