"""Tests for :mod:`src.state.evidence`."""

from __future__ import annotations

from pathlib import Path

import pytest

from state.evidence import (
    list_evidence,
    read_evidence,
    write_evidence,
    write_patch,
)
from state.schemas import (
    CoderEvidence,
    CriticEvidence,
    ExploreEvidence,
    ReviewEvidence,
    SMEEvidence,
    TestEvidence,
)


@pytest.mark.asyncio
async def test_coder_evidence_round_trip(tmp_path: Path) -> None:
    ev = CoderEvidence(
        task_id="1.1",
        diff="diff --git a b\n+x",
        files_changed=["a.py"],
        output_text="done",
        duration_s=1.5,
        success=True,
    )
    await write_evidence(tmp_path, "1.1", ev)
    loaded = await read_evidence(tmp_path, "1.1", "developer")
    assert isinstance(loaded, CoderEvidence)
    assert loaded.diff == ev.diff
    assert loaded.files_changed == ["a.py"]


@pytest.mark.asyncio
async def test_review_evidence_round_trip(tmp_path: Path) -> None:
    ev = ReviewEvidence(
        task_id="1.1",
        verdict="APPROVED",
        issues=["minor: docstring"],
        output_text="looks good",
    )
    await write_evidence(tmp_path, "1.1", ev)
    loaded = await read_evidence(tmp_path, "1.1", "review")
    assert isinstance(loaded, ReviewEvidence)
    assert loaded.verdict == "APPROVED"
    assert loaded.issues == ["minor: docstring"]


@pytest.mark.asyncio
async def test_test_evidence_round_trip(tmp_path: Path) -> None:
    ev = TestEvidence(
        task_id="1.1",
        passed=5,
        failed=0,
        total=5,
        output_text="5 passed",
        coverage_pct=82.5,
    )
    await write_evidence(tmp_path, "1.1", ev)
    loaded = await read_evidence(tmp_path, "1.1", "test")
    assert isinstance(loaded, TestEvidence)
    assert loaded.passed == 5
    assert loaded.coverage_pct == 82.5


@pytest.mark.asyncio
async def test_explore_evidence_round_trip(tmp_path: Path) -> None:
    ev = ExploreEvidence(
        task_id="plan",
        findings="x imports y",
        files_referenced=["a.py", "b.py"],
    )
    await write_evidence(tmp_path, "plan", ev)
    loaded = await read_evidence(tmp_path, "plan", "explore")
    assert isinstance(loaded, ExploreEvidence)
    assert loaded.files_referenced == ["a.py", "b.py"]


@pytest.mark.asyncio
async def test_sme_evidence_round_trip(tmp_path: Path) -> None:
    ev = SMEEvidence(
        task_id="plan",
        topic="jwt",
        findings="use RS256",
        confidence="HIGH",
    )
    await write_evidence(tmp_path, "plan", ev)
    loaded = await read_evidence(tmp_path, "plan", "domain_expert")
    assert isinstance(loaded, SMEEvidence)
    assert loaded.confidence == "HIGH"


@pytest.mark.asyncio
async def test_critic_evidence_round_trip(tmp_path: Path) -> None:
    ev = CriticEvidence(
        task_id="plan",
        verdict="NEEDS_REVISION",
        issues=["missing error handling"],
        output_text="revise",
    )
    await write_evidence(tmp_path, "plan", ev)
    loaded = await read_evidence(tmp_path, "plan", "critic")
    assert isinstance(loaded, CriticEvidence)
    assert loaded.verdict == "NEEDS_REVISION"


@pytest.mark.asyncio
async def test_discriminator_routes_correctly(tmp_path: Path) -> None:
    """Writing a coder bundle then loading must yield CoderEvidence, not any other."""
    coder = CoderEvidence(task_id="x", output_text="", success=True)
    review = ReviewEvidence(task_id="x", verdict="APPROVED")
    await write_evidence(tmp_path, "x", coder)
    await write_evidence(tmp_path, "x", review)
    a = await read_evidence(tmp_path, "x", "developer")
    b = await read_evidence(tmp_path, "x", "review")
    assert type(a).__name__ == "CoderEvidence"
    assert type(b).__name__ == "ReviewEvidence"


@pytest.mark.asyncio
async def test_missing_evidence_returns_none(tmp_path: Path) -> None:
    assert await read_evidence(tmp_path, "nope", "developer") is None


@pytest.mark.asyncio
async def test_list_evidence_returns_all_kinds_for_task(tmp_path: Path) -> None:
    await write_evidence(tmp_path, "x", CoderEvidence(task_id="x"))
    await write_evidence(tmp_path, "x", ReviewEvidence(task_id="x", verdict="APPROVED"))
    await write_evidence(tmp_path, "x", TestEvidence(task_id="x"))
    # An evidence for a different task should not appear.
    await write_evidence(tmp_path, "y", CoderEvidence(task_id="y"))
    items = await list_evidence(tmp_path, "x")
    assert len(items) == 3
    kinds = sorted(getattr(i, "kind") for i in items)
    assert kinds == ["developer", "review", "test"]


@pytest.mark.asyncio
async def test_write_patch_creates_file(tmp_path: Path) -> None:
    path = await write_patch(tmp_path, "1.1", "diff --git a/x b/x\n+hi")
    assert path.exists()
    assert path.name == "1.1.patch"
    assert "diff --git" in path.read_text()


# ---------------------------------------------------------------------------
# Extended coverage tests — edge cases for read/write/list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_evidence_missing_file(tmp_path: Path) -> None:
    """Reading evidence for a task that never had evidence returns None."""
    result = await read_evidence(tmp_path, "nonexistent_task", "developer")
    assert result is None


@pytest.mark.asyncio
async def test_read_evidence_invalid_json(tmp_path: Path) -> None:
    """Garbage content in evidence file returns None."""
    from state.paths import evidence_path

    path = evidence_path(tmp_path, "bad", "developer")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{{not valid json!!", encoding="utf-8")

    result = await read_evidence(tmp_path, "bad", "developer")
    assert result is None


@pytest.mark.asyncio
async def test_read_evidence_invalid_schema(tmp_path: Path) -> None:
    """Valid JSON but wrong schema returns None (pydantic validation fails)."""
    import json
    from state.paths import evidence_path

    path = evidence_path(tmp_path, "schema_fail", "developer")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Valid JSON but missing required fields / wrong discriminator.
    path.write_text(
        json.dumps({"kind": "nonexistent_kind", "task_id": "x"}),
        encoding="utf-8",
    )

    result = await read_evidence(tmp_path, "schema_fail", "developer")
    assert result is None


@pytest.mark.asyncio
async def test_write_patch_creates_file_extended(tmp_path: Path) -> None:
    """write_patch with arbitrary diff text creates the expected .patch file."""
    diff_text = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
    path = await write_patch(tmp_path, "2.1", diff_text)
    assert path.exists()
    assert path.name == "2.1.patch"
    content = path.read_text(encoding="utf-8")
    assert "--- a/foo.py" in content
    assert "+new" in content


@pytest.mark.asyncio
async def test_list_evidence_empty_dir(tmp_path: Path) -> None:
    """list_evidence when no evidence dir exists returns empty list."""
    items = await list_evidence(tmp_path, "anything")
    assert items == []
