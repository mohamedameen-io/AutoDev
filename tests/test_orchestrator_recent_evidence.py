"""v0.37.0 H1: tests for ``_build_recent_evidence_block``.

The helper threads reviewer / test / coder ``raw_response`` bodies into
the ``recent_evidence`` block sent to stuck-recovery prompts so the
architect-consult and sounding-board agents can refine on substance, not
just the verdict token.

Covered scenarios:
  1. Review evidence present → block contains ``REVIEWER_RAW:`` + body tail.
  2. Review evidence missing → falls back to legacy one-liner.
  3. Test evidence present but ``raw_response`` and ``output_text`` both
     empty → kind skipped silently.
  4. Per-kind cap enforcement → returned blob is the LAST ``cap`` chars.
  5. ``include_kinds=["test"]`` → only the test block appears.
  6. ``recent_evidence_max_chars_per_kind=0`` → legacy one-liner returned
     even when evidence files exist on disk.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from orchestrator.execute_phase import _build_recent_evidence_block
from state.evidence import write_evidence
from state.schemas import CoderEvidence, ReviewEvidence, Task, TestEvidence


@dataclass
class _FakeCfg:
    recent_evidence_max_chars_per_kind: int = 4000
    recent_evidence_include_kinds: list[str] | None = None

    def __post_init__(self) -> None:
        if self.recent_evidence_include_kinds is None:
            self.recent_evidence_include_kinds = ["review", "test", "coder"]


@dataclass
class _FakeOrch:
    cwd: Path
    cfg: _FakeCfg


def _make_task(task_id: str = "1.1") -> Task:
    return Task(
        id=task_id,
        phase_id="1",
        title="t",
        description="d",
        files=[],
    )


@pytest.mark.asyncio
async def test_review_present_renders_reviewer_raw_section(
    tmp_path: Path,
) -> None:
    """Reviewer ``raw_response`` is folded into the block with the
    labelled header — architect-consult sees the body, not just verdict."""
    task = _make_task()
    body = "VERDICT: NEEDS_CHANGES\nIssues:\n- Missing null-guard at line 42.\n"
    await write_evidence(
        tmp_path,
        task.id,
        ReviewEvidence(
            task_id=task.id,
            verdict="NEEDS_CHANGES",
            issues=["Missing null-guard at line 42."],
            output_text=body,
            raw_response=body,
        ),
    )
    orch = _FakeOrch(cwd=tmp_path, cfg=_FakeCfg())
    rendered = await _build_recent_evidence_block(
        orch,  # type: ignore[arg-type]
        task,
        reason="reviewer NEEDS_CHANGES",
    )
    assert "reviewer NEEDS_CHANGES" in rendered
    assert "REVIEWER_RAW:" in rendered
    assert "Missing null-guard at line 42." in rendered


@pytest.mark.asyncio
async def test_review_missing_falls_back_to_legacy_one_liner(
    tmp_path: Path,
) -> None:
    """No evidence on disk → return just the reason (with optional
    web context), no labelled sections."""
    task = _make_task()
    orch = _FakeOrch(cwd=tmp_path, cfg=_FakeCfg())
    rendered = await _build_recent_evidence_block(
        orch,  # type: ignore[arg-type]
        task,
        reason="reviewer NEEDS_CHANGES",
        web_context_block="WEB_CONTEXT: foo\n",
    )
    # Legacy shape: web_context_block + reason, no RAW headers.
    assert rendered == "WEB_CONTEXT: foo\nreviewer NEEDS_CHANGES"
    assert "REVIEWER_RAW:" not in rendered
    assert "TEST_RAW:" not in rendered
    assert "CODER_RAW:" not in rendered


@pytest.mark.asyncio
async def test_test_evidence_empty_body_is_skipped_silently(
    tmp_path: Path,
) -> None:
    """When BOTH ``raw_response`` and ``output_text`` are empty (the
    capture_failed pattern) the kind is silently dropped from the
    rendered block — no empty ``TEST_RAW:`` placeholder."""
    task = _make_task()
    await write_evidence(
        tmp_path,
        task.id,
        TestEvidence(
            task_id=task.id,
            passed=0,
            failed=0,
            total=0,
            output_text="",
            raw_response="",
        ),
    )
    # Reviewer evidence present so we have at least one section to
    # confirm the helper still rendered (and didn't bail entirely).
    await write_evidence(
        tmp_path,
        task.id,
        ReviewEvidence(
            task_id=task.id,
            verdict="NEEDS_CHANGES",
            issues=[],
            output_text="VERDICT: NEEDS_CHANGES",
            raw_response="VERDICT: NEEDS_CHANGES",
        ),
    )
    orch = _FakeOrch(cwd=tmp_path, cfg=_FakeCfg())
    rendered = await _build_recent_evidence_block(
        orch,  # type: ignore[arg-type]
        task,
        reason="tests failed",
    )
    assert "REVIEWER_RAW:" in rendered
    assert "TEST_RAW:" not in rendered


@pytest.mark.asyncio
async def test_cap_enforcement_takes_tail_of_raw_response(
    tmp_path: Path,
) -> None:
    """A 10_000-char ``raw_response`` with cap=2000 → rendered slice is
    the LAST 2000 chars (tail typically carries verdict + reasoning)."""
    task = _make_task()
    head = "X" * 8000
    tail = "Y" * 2000
    big = head + tail
    await write_evidence(
        tmp_path,
        task.id,
        ReviewEvidence(
            task_id=task.id,
            verdict="REJECTED",
            issues=[],
            output_text=big,
            raw_response=big,
        ),
    )
    orch = _FakeOrch(
        cwd=tmp_path,
        cfg=_FakeCfg(
            recent_evidence_max_chars_per_kind=2000,
            recent_evidence_include_kinds=["review"],
        ),
    )
    rendered = await _build_recent_evidence_block(
        orch,  # type: ignore[arg-type]
        task,
        reason="reviewer REJECTED",
    )
    assert "REVIEWER_RAW:" in rendered
    # Only the tail survived the cap.
    assert tail in rendered
    assert "X" * 100 not in rendered


@pytest.mark.asyncio
async def test_include_kinds_filter_emits_only_requested_section(
    tmp_path: Path,
) -> None:
    """``include_kinds=["test"]`` → only ``TEST_RAW:`` appears even if
    reviewer / coder evidence is also on disk."""
    task = _make_task()
    await write_evidence(
        tmp_path,
        task.id,
        ReviewEvidence(
            task_id=task.id,
            verdict="NEEDS_CHANGES",
            output_text="reviewer body",
            raw_response="reviewer body",
        ),
    )
    await write_evidence(
        tmp_path,
        task.id,
        TestEvidence(
            task_id=task.id,
            passed=0,
            failed=3,
            total=3,
            output_text="test body",
            raw_response="test body",
        ),
    )
    await write_evidence(
        tmp_path,
        task.id,
        CoderEvidence(
            task_id=task.id,
            output_text="coder body",
            raw_response="coder body",
        ),
    )
    orch = _FakeOrch(
        cwd=tmp_path,
        cfg=_FakeCfg(recent_evidence_include_kinds=["test"]),
    )
    rendered = await _build_recent_evidence_block(
        orch,  # type: ignore[arg-type]
        task,
        reason="tests failed",
    )
    assert "TEST_RAW:" in rendered
    assert "test body" in rendered
    assert "REVIEWER_RAW:" not in rendered
    assert "CODER_RAW:" not in rendered


@pytest.mark.asyncio
async def test_cap_zero_returns_legacy_one_liner_ignoring_evidence(
    tmp_path: Path,
) -> None:
    """``cap=0`` → legacy behaviour even with evidence on disk; the
    operator's escape hatch for tight token budgets."""
    task = _make_task()
    await write_evidence(
        tmp_path,
        task.id,
        ReviewEvidence(
            task_id=task.id,
            verdict="REJECTED",
            output_text="this body MUST NOT appear in the rendered block",
            raw_response="this body MUST NOT appear in the rendered block",
        ),
    )
    orch = _FakeOrch(
        cwd=tmp_path,
        cfg=_FakeCfg(recent_evidence_max_chars_per_kind=0),
    )
    rendered = await _build_recent_evidence_block(
        orch,  # type: ignore[arg-type]
        task,
        reason="reviewer REJECTED",
    )
    assert rendered == "reviewer REJECTED"
    assert "REVIEWER_RAW:" not in rendered
    assert "this body" not in rendered
