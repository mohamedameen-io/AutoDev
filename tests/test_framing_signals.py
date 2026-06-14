"""Deterministic framing-signals tests (Phase 2)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orchestrator.framing_signals import (
    _is_autodev_commit,
    compute_boundary_repeatedly_touched,
    compute_recurrence_at_seam,
)
from state.evidence import write_evidence
from state.file_index import CandidateDigest, FileHit, SymbolHit
from state.ledger import append_entry
from state.schemas import CoderEvidence


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )


def _init_repo(cwd: Path) -> None:
    _git(cwd, "init", "-q")
    _git(cwd, "config", "user.email", "t@t")
    _git(cwd, "config", "user.name", "t")


def _commit(cwd: Path, path: str, content: str, msg: str) -> None:
    fp = cwd / path
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content)
    _git(cwd, "add", "-A")
    _git(cwd, "commit", "-qm", msg)


def _digest(*files: str) -> CandidateDigest:
    return CandidateDigest(file_hits=[FileHit(path=f, lang="py") for f in files])


def test_autodev_commit_pattern_matching() -> None:
    assert _is_autodev_commit("autodev: task t-1 (Fix)")
    assert not _is_autodev_commit("fix: x")
    assert not _is_autodev_commit("Merge PR #200")
    assert not _is_autodev_commit("autodev-inspired: task")


@pytest.mark.asyncio
async def test_recurrence_excludes_autodev_commits(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "src/foo.py", "v1\n", "fix: human one")
    _commit(tmp_path, "src/foo.py", "v2\n", "autodev: task t-1 (auto)")
    _commit(tmp_path, "src/foo.py", "v3\n", "refactor: human two")
    count, shas, sig = await compute_recurrence_at_seam(tmp_path, _digest("src/foo.py"))
    assert count == 2
    assert sig.fired
    assert sig.name == "recurrence_at_seam"


@pytest.mark.asyncio
async def test_recurrence_empty_repo(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "other.py", "x\n", "init")
    count, shas, sig = await compute_recurrence_at_seam(tmp_path, _digest("src/foo.py"))
    assert count == 0
    assert shas == []
    assert not sig.fired


@pytest.mark.asyncio
async def test_recurrence_timeout_degrades(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    for i in range(20):
        _commit(tmp_path, "src/foo.py", f"v{i}\n", f"fix: change {i}")
    count, shas, sig = await compute_recurrence_at_seam(
        tmp_path, _digest("src/foo.py"), timeout_s=0.0001
    )
    assert count == 0
    assert not sig.fired
    assert sig.confidence == 0.0


@pytest.mark.asyncio
async def test_recurrence_reads_digest_object_fields(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "src/sym.py", "def f(): pass\n", "fix: human one")
    _commit(tmp_path, "src/filehit.py", "y\n", "fix: human two")
    digest = CandidateDigest(
        symbol_hits=[
            SymbolHit(
                name="f",
                kind="function",
                file_path="src/sym.py",
                line=1,
                signature="def f()",
            )
        ],
        file_hits=[FileHit(path="src/filehit.py", lang="py")],
    )
    count, shas, sig = await compute_recurrence_at_seam(tmp_path, digest)
    # Both the symbol_hit file and the file_hit path are scoped into git log.
    assert count == 2
    assert sig.fired


@pytest.mark.asyncio
async def test_boundary_fires_on_overlap(tmp_path: Path) -> None:
    await write_evidence(
        tmp_path,
        "1.1",
        CoderEvidence(task_id="1.1", files_changed=["src/bar.py"], success=True),
    )
    await append_entry(
        tmp_path, "update_task_status", {"task_id": "1.1", "status": "complete"}, "s"
    )
    await write_evidence(
        tmp_path,
        "1.2",
        CoderEvidence(
            task_id="1.2", files_changed=["src/bar.py", "src/x.py"], success=True
        ),
    )
    await append_entry(
        tmp_path, "update_task_status", {"task_id": "1.2", "status": "complete"}, "s"
    )
    count, sig = await compute_boundary_repeatedly_touched(tmp_path, _digest("src/bar.py"))
    assert count == 2
    assert sig.fired
    assert sig.name == "boundary_repeatedly_touched"


@pytest.mark.asyncio
async def test_boundary_zero_on_fresh_files(tmp_path: Path) -> None:
    await write_evidence(
        tmp_path,
        "1.1",
        CoderEvidence(task_id="1.1", files_changed=["src/bar.py"], success=True),
    )
    await append_entry(
        tmp_path, "update_task_status", {"task_id": "1.1", "status": "complete"}, "s"
    )
    count, sig = await compute_boundary_repeatedly_touched(
        tmp_path, _digest("src/fresh.py")
    )
    assert count == 0
    assert not sig.fired


@pytest.mark.asyncio
async def test_boundary_missing_evidence_skips(tmp_path: Path) -> None:
    # ledger says complete but the developer evidence file is absent.
    await append_entry(
        tmp_path, "update_task_status", {"task_id": "1.1", "status": "complete"}, "s"
    )
    count, sig = await compute_boundary_repeatedly_touched(tmp_path, _digest("src/bar.py"))
    assert count == 0
    assert not sig.fired
