"""autodev status — framing summary line (Phase 5)."""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console

from cli.commands.status import _print_framing_summary
from state.evidence import write_evidence
from state.schemas import FramingEvidence, SolutionApproach


@pytest.mark.asyncio
async def test_status_prints_framing_classification(tmp_path: Path) -> None:
    sa = SolutionApproach(
        name="redesign",
        altitude="design_fix",
        summary="separate planes",
        eliminates_failure_class=True,
        primary_tradeoff="t",
        primary_risk="r",
        est_blast_radius="cross-module contract",
    )
    ev = FramingEvidence(
        task_id="plan-framing",
        classification="realized_design_failure",
        confidence=0.9,
        hypothesis_challenged="h",
        approaches=[sa],
        chosen_approach_name="redesign",
        altitude_rationale="r",
    )
    await write_evidence(tmp_path, "plan-framing", ev)
    console = Console()
    with console.capture() as cap:
        await _print_framing_summary(console, tmp_path)
    out = cap.get()
    assert "realized_design_failure" in out
    assert "design_fix" in out


@pytest.mark.asyncio
async def test_status_framing_summary_absent_when_no_evidence(tmp_path: Path) -> None:
    console = Console()
    with console.capture() as cap:
        await _print_framing_summary(console, tmp_path)
    assert cap.get().strip() == ""
