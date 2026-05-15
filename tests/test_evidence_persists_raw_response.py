"""Verify ReviewEvidence (and friends) carry ``raw_response`` round-trip.

v0.31.0 (Phase 1.2): each agent-evidence variant gained an optional
``raw_response: str | None`` field. The orchestrator writes it
explicitly even when ``output_text`` ends up empty so post-mortems can
answer "what did the model actually return?" without grepping
``.autodev/debug/`` dumps.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from state.evidence import read_evidence, write_evidence
from state.schemas import CoderEvidence, ReviewEvidence, TestEvidence


@pytest.mark.asyncio
async def test_review_evidence_carries_raw_response(tmp_path: Path) -> None:
    """Round-trip: raw_response survives write → read."""
    ev = ReviewEvidence(
        task_id="1.1",
        verdict="APPROVED",
        issues=[],
        output_text="VERDICT: APPROVED\n",
        raw_response="VERDICT: APPROVED\n",
    )
    await write_evidence(tmp_path, "1.1", ev)
    loaded = await read_evidence(tmp_path, "1.1", "review")
    assert isinstance(loaded, ReviewEvidence)
    assert loaded.raw_response == "VERDICT: APPROVED\n"


@pytest.mark.asyncio
async def test_review_evidence_raw_response_when_output_text_empty(
    tmp_path: Path,
) -> None:
    """The point of the field: carry raw text even when output_text is empty.

    Models that produce an empty ``result`` on the happy path (the
    Hypothesis A failure mode) should still leave a forensic trail in
    the evidence record itself.
    """
    ev = ReviewEvidence(
        task_id="1.1",
        verdict="MALFORMED",
        issues=["empty reviewer response"],
        output_text="",
        raw_response="",  # explicitly preserved as the empty string
    )
    await write_evidence(tmp_path, "1.1", ev)
    loaded = await read_evidence(tmp_path, "1.1", "review")
    assert isinstance(loaded, ReviewEvidence)
    assert loaded.raw_response == ""
    assert loaded.verdict == "MALFORMED"


@pytest.mark.asyncio
async def test_review_evidence_raw_response_defaults_to_none(
    tmp_path: Path,
) -> None:
    """Backward compat: existing evidence files without raw_response load."""
    ev = ReviewEvidence(
        task_id="1.1",
        verdict="APPROVED",
        output_text="legacy output",
    )
    await write_evidence(tmp_path, "1.1", ev)
    loaded = await read_evidence(tmp_path, "1.1", "review")
    assert isinstance(loaded, ReviewEvidence)
    assert loaded.raw_response is None


@pytest.mark.asyncio
async def test_coder_evidence_carries_raw_response(tmp_path: Path) -> None:
    """Symmetric: developer evidence also carries raw_response."""
    ev = CoderEvidence(
        task_id="1.2",
        diff="diff --git a b\n+x",
        files_changed=["a.py"],
        output_text="done",
        raw_response="done",
    )
    await write_evidence(tmp_path, "1.2", ev)
    loaded = await read_evidence(tmp_path, "1.2", "developer")
    assert isinstance(loaded, CoderEvidence)
    assert loaded.raw_response == "done"


@pytest.mark.asyncio
async def test_test_evidence_carries_raw_response(tmp_path: Path) -> None:
    """Symmetric: test_engineer evidence also carries raw_response."""
    ev = TestEvidence(
        task_id="1.3",
        passed=5,
        failed=0,
        total=5,
        output_text="RESULTS: passed=5 failed=0 total=5",
        raw_response="RESULTS: passed=5 failed=0 total=5",
    )
    await write_evidence(tmp_path, "1.3", ev)
    loaded = await read_evidence(tmp_path, "1.3", "test")
    assert isinstance(loaded, TestEvidence)
    assert loaded.raw_response == "RESULTS: passed=5 failed=0 total=5"


def test_review_evidence_accepts_malformed_verdict() -> None:
    """Schema-level: MALFORMED is a valid verdict value."""
    ev = ReviewEvidence(
        task_id="1.4",
        verdict="MALFORMED",
        issues=["empty reviewer response"],
    )
    assert ev.verdict == "MALFORMED"
