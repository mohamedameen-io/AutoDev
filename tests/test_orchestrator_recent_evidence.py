"""v0.37.0 H1 / v0.38.0 HK1+HK2: tests for ``_build_recent_evidence_block``.

The helper threads reviewer / test / developer ``raw_response`` bodies
into the ``recent_evidence`` block sent to stuck-recovery prompts so the
architect-consult and sounding-board agents can refine on substance, not
just the verdict token.

Covered scenarios:
  1. Review evidence present → block contains ``REVIEWER_RAW:`` + body tail.
  2. Review evidence missing → falls back to legacy one-liner.
  3. Test evidence present but ``raw_response`` and ``output_text`` both
     empty → kind skipped silently.
  4. Per-kind cap enforcement → reviewer/test = LAST ``cap`` chars;
     developer (HK2) = head + tail with explicit truncation marker.
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
            self.recent_evidence_include_kinds = ["review", "test", "developer"]


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
    assert "DEVELOPER_RAW:" not in rendered


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
    assert "DEVELOPER_RAW:" not in rendered


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


# ---------------------------------------------------------------------------
# v0.38.0 HK2: developer body truncates head + tail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_developer_body_truncates_head_and_tail_with_marker(
    tmp_path: Path,
) -> None:
    """v0.38.0 HK2: developer ``raw_response`` exceeding the per-kind
    cap is truncated to ``head[:cap//2] + marker + tail[-cap//2:]`` so
    the architect sees both the failing call site (typically near the
    top of a long tool transcript) AND the final error (near the bottom).
    Reviewer / test bodies still tail-only.
    """
    task = _make_task()
    head_payload = "H" * 500
    middle_payload = "M" * 800
    tail_payload = "T" * 500
    big = head_payload + middle_payload + tail_payload  # 1800 chars total

    await write_evidence(
        tmp_path,
        task.id,
        CoderEvidence(
            task_id=task.id,
            output_text=big,
            raw_response=big,
        ),
    )
    cap = 200  # → half = 100
    orch = _FakeOrch(
        cwd=tmp_path,
        cfg=_FakeCfg(
            recent_evidence_max_chars_per_kind=cap,
            recent_evidence_include_kinds=["developer"],
        ),
    )
    rendered = await _build_recent_evidence_block(
        orch,  # type: ignore[arg-type]
        task,
        reason="developer failed",
    )
    assert "DEVELOPER_RAW:" in rendered
    # head: first 100 H chars survive (the call site).
    assert "H" * 100 in rendered
    # tail: last 100 T chars survive (the final error).
    assert "T" * 100 in rendered
    # middle (the 800 'M' chars) is dropped.
    assert "M" * 100 not in rendered
    # truncation marker present with byte count = 1800 - 200 = 1600.
    assert "[...truncated 1600 bytes...]" in rendered


@pytest.mark.asyncio
async def test_review_tail_only_truncation_unchanged_by_hk2(
    tmp_path: Path,
) -> None:
    """v0.38.0 HK2 regression-guard: reviewer body still tail-only —
    head + tail is developer-specific. Same body length, same cap,
    different label_kind path."""
    task = _make_task()
    head_payload = "H" * 500
    middle_payload = "M" * 800
    tail_payload = "T" * 500
    big = head_payload + middle_payload + tail_payload

    await write_evidence(
        tmp_path,
        task.id,
        ReviewEvidence(
            task_id=task.id,
            verdict="REJECTED",
            output_text=big,
            raw_response=big,
        ),
    )
    cap = 200
    orch = _FakeOrch(
        cwd=tmp_path,
        cfg=_FakeCfg(
            recent_evidence_max_chars_per_kind=cap,
            recent_evidence_include_kinds=["review"],
        ),
    )
    rendered = await _build_recent_evidence_block(
        orch,  # type: ignore[arg-type]
        task,
        reason="reviewer REJECTED",
    )
    assert "REVIEWER_RAW:" in rendered
    # Reviewer = tail-only. Head H chars dropped, marker NOT present.
    assert "H" * 100 not in rendered
    # Last 200 T chars survive.
    assert "T" * 200 in rendered
    assert "[...truncated" not in rendered


@pytest.mark.asyncio
async def test_developer_body_under_cap_emits_no_marker(
    tmp_path: Path,
) -> None:
    """v0.38.0 HK2: when the body fits within the cap, no truncation
    marker should appear — the head+tail branch is reserved for
    over-cap bodies."""
    task = _make_task()
    small_body = "small developer body — fits within cap easily"

    await write_evidence(
        tmp_path,
        task.id,
        CoderEvidence(
            task_id=task.id,
            output_text=small_body,
            raw_response=small_body,
        ),
    )
    orch = _FakeOrch(
        cwd=tmp_path,
        cfg=_FakeCfg(
            recent_evidence_max_chars_per_kind=4000,
            recent_evidence_include_kinds=["developer"],
        ),
    )
    rendered = await _build_recent_evidence_block(
        orch,  # type: ignore[arg-type]
        task,
        reason="developer failed",
    )
    assert "DEVELOPER_RAW:" in rendered
    assert small_body in rendered
    assert "[...truncated" not in rendered
