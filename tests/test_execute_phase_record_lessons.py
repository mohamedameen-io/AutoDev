"""Tests for execute_phase._record_lessons extraction + recording."""

from __future__ import annotations

import pytest

from orchestrator.execute_phase import _record_lessons


class _FakeKnowledge:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, float]] = []

    async def record(self, text: str, role: str, confidence: float = 0.7) -> None:
        self.calls.append((text, role, confidence))


class _FakeOrchestrator:
    def __init__(self) -> None:
        self.knowledge = _FakeKnowledge()


@pytest.mark.asyncio
async def test_record_lessons_extracts_single_lesson_line() -> None:
    orch = _FakeOrchestrator()
    output = "context\nLESSON: prefer explicit typing\nmore context"
    await _record_lessons(orch, "t1", output, "developer")
    assert orch.knowledge.calls == [("prefer explicit typing", "developer", 0.7)]


@pytest.mark.asyncio
async def test_record_lessons_extracts_multiple_lessons() -> None:
    orch = _FakeOrchestrator()
    output = "LESSON: first\nnoise\nLESSON: second\n"
    await _record_lessons(orch, "t1", output, "reviewer")
    assert [c[0] for c in orch.knowledge.calls] == ["first", "second"]
    assert all(c[1] == "reviewer" for c in orch.knowledge.calls)


@pytest.mark.asyncio
async def test_record_lessons_prefix_is_case_insensitive() -> None:
    orch = _FakeOrchestrator()
    await _record_lessons(orch, "t1", "lesson: lowercase works", "developer")
    assert orch.knowledge.calls == [("lowercase works", "developer", 0.7)]


@pytest.mark.asyncio
async def test_record_lessons_ignores_empty_lesson_entries() -> None:
    orch = _FakeOrchestrator()
    await _record_lessons(orch, "t1", "LESSON:  \nLESSON: real", "developer")
    assert [c[0] for c in orch.knowledge.calls] == ["real"]


@pytest.mark.asyncio
async def test_record_lessons_no_lesson_lines_no_recording() -> None:
    orch = _FakeOrchestrator()
    await _record_lessons(orch, "t1", "regular output without lessons", "developer")
    assert orch.knowledge.calls == []


@pytest.mark.asyncio
async def test_record_lessons_record_error_does_not_raise() -> None:
    class _FailingKnowledge:
        async def record(
            self, text: str, role: str, confidence: float = 0.7
        ) -> None:
            raise RuntimeError("store unavailable")

    class _FailingOrchestrator:
        def __init__(self) -> None:
            self.knowledge = _FailingKnowledge()

    await _record_lessons(_FailingOrchestrator(), "t1", "LESSON: keep going", "developer")

