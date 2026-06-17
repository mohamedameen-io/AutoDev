"""Framing taxonomy + scale-aware altitude tests (WS2-17, gate S4 framing-side).

Two defects under test:

DEFECT 1 (WS2-17): the framing taxonomy was BINARY+bug-shaped — the classifier
regex only matched ``local_defect|realized_design_failure``, so feature / refactor
/ greenfield work exited framing mislabeled as ``local_defect`` (field probe P4: a
feature classified ``local_defect``). The vocabulary must include ``feature`` /
``refactor`` / ``greenfield`` while keeping the two bug classes.

DEFECT 2 (S4 framing-side): framing must NOT unconditionally select the
``local_patch`` altitude when the repo is large. The (parallel) scale agent threads
a ``scale_context`` dict into framing; when its scale signals are high, framing must
NOT force the lowest altitude.

scale_context shape consumed (must match the scale agent):
    {'is_large': bool, 'depth_max': int, 'avg_file_size_bytes': int}
Absent ``scale_context`` => behaves as today (backward compatible).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator.framing_phase import run_framing_phase
from stub_adapter import StubAdapter, ok


def _bootstrap_repo(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=str(tmp_path), check=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(tmp_path), check=True)
    (tmp_path / "main.py").write_text("def main():\n    return 1\n")
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(tmp_path), check=True)


def _make_orch(cwd: Path, adapter: StubAdapter) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    registry = build_registry(cfg)
    return Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-taxonomy",
    )


def _framing_text(
    classification: str,
    confidence: float = 0.0,
    signals: str = "none",
    hypothesis: str = "stub hypothesis",
) -> str:
    return (
        "```framing\n"
        f"CLASSIFICATION: {classification}\n"
        f"CONFIDENCE: {confidence}\n"
        f"HYPOTHESIS_CHALLENGED: {hypothesis}\n"
        f"SIGNALS_FIRED: {signals}\n"
        "```\n"
    )


# --- DEFECT 1: taxonomy ------------------------------------------------------


@pytest.mark.asyncio
async def test_feature_spec_classified_as_feature(tmp_path: Path) -> None:
    """A feature spec must classify as 'feature' — NOT forced to local_defect."""
    _bootstrap_repo(tmp_path)
    adapter = StubAdapter({"framing": ok(_framing_text("feature", 0.0))})
    orch = _make_orch(tmp_path, adapter)
    decision = await run_framing_phase(
        orch, "Add a new export-to-CSV feature", "", "", None, "abc123"
    )
    assert decision is not None
    assert decision.classification == "feature"


@pytest.mark.asyncio
async def test_refactor_spec_classified_as_refactor(tmp_path: Path) -> None:
    """A refactor spec must classify as 'refactor' — NOT forced to local_defect."""
    _bootstrap_repo(tmp_path)
    adapter = StubAdapter({"framing": ok(_framing_text("refactor", 0.0))})
    orch = _make_orch(tmp_path, adapter)
    decision = await run_framing_phase(
        orch, "Refactor the payments module for clarity", "", "", None, "abc123"
    )
    assert decision is not None
    assert decision.classification == "refactor"


@pytest.mark.asyncio
async def test_greenfield_spec_classified_as_greenfield(tmp_path: Path) -> None:
    """A greenfield spec must classify as 'greenfield' — NOT forced to local_defect."""
    _bootstrap_repo(tmp_path)
    adapter = StubAdapter({"framing": ok(_framing_text("greenfield", 0.0))})
    orch = _make_orch(tmp_path, adapter)
    decision = await run_framing_phase(
        orch, "Build a brand-new CLI from scratch", "", "", None, "abc123"
    )
    assert decision is not None
    assert decision.classification == "greenfield"


@pytest.mark.asyncio
async def test_bug_classes_still_supported(tmp_path: Path) -> None:
    """The original bug classes must still parse (no regression)."""
    _bootstrap_repo(tmp_path)
    adapter = StubAdapter({"framing": ok(_framing_text("local_defect", 0.2))})
    orch = _make_orch(tmp_path, adapter)
    decision = await run_framing_phase(orch, "fix the null deref", "", "", None, "abc")
    assert decision is not None
    assert decision.classification == "local_defect"


# --- DEFECT 2: scale-aware altitude (gate S4 framing-side) -------------------

# scale_context the scale agent threads in. Keys per coordination contract.
_LARGE_SCALE = {"is_large": True, "depth_max": 14, "avg_file_size_bytes": 48_000}
_SMALL_SCALE = {"is_large": False, "depth_max": 3, "avg_file_size_bytes": 2_000}


@pytest.mark.asyncio
async def test_large_scale_does_not_force_local_patch(tmp_path: Path) -> None:
    """S4: a large-repo scale_context must NOT unconditionally pick local_patch.

    The classifier returns a feature with no structural design signal; on a SMALL
    repo that would degrade to local_patch (the lowest altitude). With a LARGE
    scale_context, framing must not force the lowest altitude.
    """
    _bootstrap_repo(tmp_path)
    adapter = StubAdapter({"framing": ok(_framing_text("feature", 0.0))})
    orch = _make_orch(tmp_path, adapter)
    decision = await run_framing_phase(
        orch,
        "Add a new export-to-CSV feature",
        "",
        "",
        None,
        "abc123",
        scale_context=_LARGE_SCALE,
    )
    assert decision is not None
    # The chosen approach must not be the lowest (local_patch) altitude when the
    # repo is large.
    assert decision.chosen_approach.altitude != "local_patch"


@pytest.mark.asyncio
async def test_small_scale_is_backward_compatible(tmp_path: Path) -> None:
    """A small / absent scale_context behaves as today (local_patch default)."""
    _bootstrap_repo(tmp_path)
    adapter = StubAdapter({"framing": ok(_framing_text("local_defect", 0.0))})
    orch = _make_orch(tmp_path, adapter)
    decision = await run_framing_phase(
        orch, "fix x", "", "", None, "abc123", scale_context=_SMALL_SCALE
    )
    assert decision is not None
    assert decision.chosen_approach.altitude == "local_patch"


@pytest.mark.asyncio
async def test_absent_scale_context_is_backward_compatible(tmp_path: Path) -> None:
    """No scale_context at all => unchanged behavior (local_patch)."""
    _bootstrap_repo(tmp_path)
    adapter = StubAdapter({"framing": ok(_framing_text("local_defect", 0.0))})
    orch = _make_orch(tmp_path, adapter)
    decision = await run_framing_phase(orch, "fix x", "", "", None, "abc123")
    assert decision is not None
    assert decision.chosen_approach.altitude == "local_patch"
