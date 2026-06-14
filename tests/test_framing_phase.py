"""run_framing_phase tests — classifier, generation, panel, resume, gates.

Filled incrementally across Phases 2-6.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator.framing_phase import (
    _extract_classification,
    run_framing_phase,
)
from state.evidence import read_evidence, write_evidence
from state.ledger import read_entries
from state.schemas import FramingEvidence, SolutionApproach
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
        session_id="sess-framing",
    )


def _framing_text(
    classification: str = "local_defect",
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


_LOCAL_ITEM = (
    "- name: trim\n"
    "  altitude: local_patch\n"
    "  summary: trim the observation\n"
    "  eliminates_failure_class: false\n"
    "  primary_tradeoff: fast but only bounds\n"
    "  primary_risk: recurs at the seam\n"
    "  integration_surface: [src/foo.py]\n"
    "  est_blast_radius: single function"
)
_DESIGN_ITEM = (
    "- name: separate-planes\n"
    "  altitude: design_fix\n"
    "  summary: separate control and data planes\n"
    "  eliminates_failure_class: true\n"
    "  primary_tradeoff: larger diff now\n"
    "  primary_risk: cross-module contract\n"
    "  integration_surface: [src/core.py]\n"
    "  est_blast_radius: cross-module contract"
)


def _approaches_text(*items: str) -> str:
    return "```approaches\n" + "\n".join(items) + "\n```\n"


def _force_structural(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake(cwd: object, candidate_digest: object, intent: str):
        return ["recurrence_at_seam"], True

    monkeypatch.setattr("orchestrator.framing_phase._compute_signals", _fake)


# --- parser -----------------------------------------------------------------


def test_parse_framing_response_empty() -> None:
    cls, conf, diags = _extract_classification("")
    assert cls == "local_defect"
    assert conf == 0.0
    assert any("empty response" in d for d in diags)


def test_parse_framing_response_missing_classification() -> None:
    cls, conf, diags = _extract_classification("some prose with no verdict line")
    assert cls == "local_defect"
    assert conf == 0.0


# --- conservatism gate ------------------------------------------------------


@pytest.mark.asyncio
async def test_conservatism_gate_low_confidence(tmp_path: Path) -> None:
    _bootstrap_repo(tmp_path)
    adapter = StubAdapter(
        {"framing": ok(_framing_text("realized_design_failure", 0.6, "recurrence_at_seam"))}
    )
    orch = _make_orch(tmp_path, adapter)
    decision = await run_framing_phase(
        orch, "trim the tool observation", "", "", None, "abc123"
    )
    assert decision is not None
    assert decision.classification == "local_defect"
    assert len(decision.approaches) == 1
    assert decision.approaches[0].altitude == "local_patch"
    assert adapter.count("altitude_judge") == 0


@pytest.mark.asyncio
async def test_conservatism_gate_no_structural_signal(tmp_path: Path) -> None:
    _bootstrap_repo(tmp_path)
    adapter = StubAdapter(
        {"framing": ok(_framing_text("realized_design_failure", 0.9, "none"))}
    )
    orch = _make_orch(tmp_path, adapter)
    decision = await run_framing_phase(orch, "x", "", "", None, "abc123")
    assert decision is not None
    assert decision.classification == "local_defect"
    assert adapter.count("altitude_judge") == 0


# --- evidence + ledger ------------------------------------------------------


@pytest.mark.asyncio
async def test_framing_writes_evidence(tmp_path: Path) -> None:
    _bootstrap_repo(tmp_path)
    adapter = StubAdapter({"framing": ok(_framing_text("local_defect", 0.2))})
    orch = _make_orch(tmp_path, adapter)
    await run_framing_phase(orch, "x", "", "", None, "abc123")
    ev = await read_evidence(tmp_path, "plan-framing", "framing")
    assert isinstance(ev, FramingEvidence)
    assert ev.classification == "local_defect"
    assert (tmp_path / ".autodev" / "evidence" / "plan-framing-framing.json").exists()


@pytest.mark.asyncio
async def test_framing_ledger_classified_op(tmp_path: Path) -> None:
    _bootstrap_repo(tmp_path)
    adapter = StubAdapter({"framing": ok(_framing_text("local_defect", 0.1))})
    orch = _make_orch(tmp_path, adapter)
    await run_framing_phase(orch, "x", "", "", None, "abc123")
    ops = [e.op for e in read_entries(tmp_path)]
    assert "framing_classified" in ops


# --- dispatch ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_framing_dispatch_uses_specialist_path_not_registry(tmp_path: Path) -> None:
    _bootstrap_repo(tmp_path)
    adapter = StubAdapter({"framing": ok(_framing_text("local_defect", 0.1))})
    orch = _make_orch(tmp_path, adapter)
    # framing is deliberately NOT in the registry (build_registry only iterates
    # REQUIRED_AGENT_ROLES); dispatch must bypass it via the specialist path.
    assert "framing" not in orch.registry
    await run_framing_phase(orch, "x", "", "", None, "abc123")
    assert adapter.count("framing") == 1
    assert any(c.role == "framing" for c in adapter.calls)


# --- kill switches ----------------------------------------------------------


@pytest.mark.asyncio
async def test_framing_disabled_via_config(tmp_path: Path) -> None:
    _bootstrap_repo(tmp_path)
    adapter = StubAdapter({})
    orch = _make_orch(tmp_path, adapter)
    orch.cfg.framing.enabled = False
    decision = await run_framing_phase(orch, "x", "", "", None, "abc123")
    assert decision is None
    assert adapter.count("framing") == 0


@pytest.mark.asyncio
async def test_framing_disabled_via_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bootstrap_repo(tmp_path)
    monkeypatch.setenv("AUTODEV_FRAMING_DISABLED", "1")
    adapter = StubAdapter({})
    orch = _make_orch(tmp_path, adapter)
    decision = await run_framing_phase(orch, "x", "", "", None, "abc123")
    assert decision is None
    assert adapter.count("framing") == 0


# --- resume / determinism ---------------------------------------------------


@pytest.mark.asyncio
async def test_framing_resume_skips_classifier(tmp_path: Path) -> None:
    _bootstrap_repo(tmp_path)
    sa = SolutionApproach(
        name="local_patch",
        altitude="local_patch",
        summary="s",
        eliminates_failure_class=False,
        primary_tradeoff="t",
        primary_risk="r",
        est_blast_radius="single function",
    )
    ev = FramingEvidence(
        task_id="plan-framing",
        classification="local_defect",
        confidence=0.0,
        hypothesis_challenged="h",
        approaches=[sa],
        chosen_approach_name="local_patch",
    )
    await write_evidence(tmp_path, "plan-framing", ev)
    adapter = StubAdapter({})  # empty: any framing call would be a fallback
    orch = _make_orch(tmp_path, adapter)
    decision = await run_framing_phase(orch, "x", "", "", None, "abc123")
    assert adapter.count("framing") == 0
    assert decision is not None
    assert decision.classification == "local_defect"
    assert decision.chosen_approach.name == "local_patch"


@pytest.mark.asyncio
async def test_framing_byte_identical_stub(tmp_path: Path) -> None:
    text = _framing_text("local_defect", 0.0)
    d1 = tmp_path / "r1"
    d2 = tmp_path / "r2"
    d1.mkdir()
    d2.mkdir()
    _bootstrap_repo(d1)
    _bootstrap_repo(d2)
    o1 = _make_orch(d1, StubAdapter({"framing": ok(text)}))
    await run_framing_phase(o1, "x", "", "", None, "abc123")
    o2 = _make_orch(d2, StubAdapter({"framing": ok(text)}))
    await run_framing_phase(o2, "x", "", "", None, "abc123")
    raw1 = json.loads(
        (d1 / ".autodev" / "evidence" / "plan-framing-framing.json").read_text()
    )
    raw2 = json.loads(
        (d2 / ".autodev" / "evidence" / "plan-framing-framing.json").read_text()
    )
    assert raw1 == raw2


# --- Phase 3: parse_approaches + design-path generation ---------------------


def test_parse_approaches_empty() -> None:
    from orchestrator.framing_phase import parse_approaches

    approaches, failures = parse_approaches("")
    assert approaches == []
    assert any("empty response" in f for f in failures)


def test_parse_approaches_valid_two() -> None:
    from orchestrator.framing_phase import parse_approaches

    approaches, failures = parse_approaches(_approaches_text(_LOCAL_ITEM, _DESIGN_ITEM))
    assert len(approaches) == 2
    assert failures == []
    assert {a.altitude for a in approaches} == {"local_patch", "design_fix"}


def test_parse_approaches_missing_eliminates_field() -> None:
    from orchestrator.framing_phase import parse_approaches

    bad = _LOCAL_ITEM.replace("  eliminates_failure_class: false\n", "")
    approaches, failures = parse_approaches(_approaches_text(bad, _DESIGN_ITEM))
    assert len(approaches) == 1
    assert approaches[0].altitude == "design_fix"
    assert any("malformed approach" in f for f in failures)


def test_parse_approaches_unknown_field_skipped() -> None:
    from orchestrator.framing_phase import parse_approaches

    bad = _LOCAL_ITEM + "\n  bogus_field: x"
    approaches, failures = parse_approaches(_approaches_text(bad, _DESIGN_ITEM))
    assert len(approaches) == 1
    assert approaches[0].altitude == "design_fix"
    assert any("malformed approach" in f for f in failures)


def test_parse_approaches_respects_num_approaches_cap() -> None:
    from orchestrator.framing_phase import parse_approaches

    items = [
        _LOCAL_ITEM,
        _DESIGN_ITEM,
        _DESIGN_ITEM.replace("separate-planes", "p3"),
        _DESIGN_ITEM.replace("separate-planes", "p4"),
    ]
    approaches, failures = parse_approaches(_approaches_text(*items), num_approaches=3)
    assert len(approaches) == 3
    assert any("truncated" in f for f in failures)


@pytest.mark.asyncio
async def test_design_path_generates_both_altitudes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bootstrap_repo(tmp_path)
    _force_structural(monkeypatch)
    text = _framing_text("realized_design_failure", 0.85) + _approaches_text(
        _LOCAL_ITEM, _DESIGN_ITEM
    )
    adapter = StubAdapter({"framing": ok(text)})
    orch = _make_orch(tmp_path, adapter)
    decision = await run_framing_phase(orch, "trim it", "", "", None, "abc123")
    assert decision is not None
    assert decision.classification == "realized_design_failure"
    altitudes = {a.altitude for a in decision.approaches}
    assert "local_patch" in altitudes
    assert "design_fix" in altitudes
    ops = [e.op for e in read_entries(tmp_path)]
    assert "framing_strategy_chosen" in ops


@pytest.mark.asyncio
async def test_design_path_degrades_when_only_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bootstrap_repo(tmp_path)
    _force_structural(monkeypatch)
    text = _framing_text("realized_design_failure", 0.85) + _approaches_text(_LOCAL_ITEM)
    adapter = StubAdapter({"framing": ok(text)})
    orch = _make_orch(tmp_path, adapter)
    decision = await run_framing_phase(orch, "trim it", "", "", None, "abc123")
    assert decision is not None
    assert decision.classification == "local_defect"
    assert len(decision.approaches) == 1
    assert decision.approaches[0].altitude == "local_patch"
    ev = await read_evidence(tmp_path, "plan-framing", "framing")
    assert isinstance(ev, FramingEvidence)
    assert "parse_degraded" in ev.signals_fired


@pytest.mark.asyncio
async def test_framing_call_count_design_path_is_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bootstrap_repo(tmp_path)
    _force_structural(monkeypatch)
    text = _framing_text("realized_design_failure", 0.85) + _approaches_text(
        _LOCAL_ITEM, _DESIGN_ITEM
    )
    adapter = StubAdapter({"framing": ok(text)})
    orch = _make_orch(tmp_path, adapter)
    await run_framing_phase(orch, "trim it", "", "", None, "abc123")
    assert adapter.count("framing") == 1


# --- Phase 4: altitude_judge Borda panel ------------------------------------


def _design_text() -> str:
    return _framing_text("realized_design_failure", 0.85) + _approaches_text(
        _LOCAL_ITEM, _DESIGN_ITEM
    )


def _slots_in(prompt: str) -> dict[int, str]:
    return {
        int(m.group(1)): m.group(2)
        for m in re.finditer(r"### Candidate (\d+)\naltitude: (\w+)", prompt)
    }


def _judge_ranks_design_first(inv):  # type: ignore[no-untyped-def]
    slots = _slots_in(inv.prompt)
    design_slot = next((s for s, alt in slots.items() if alt == "design_fix"), 1)
    others = [s for s in sorted(slots) if s != design_slot]
    ranking = " ".join(str(s) for s in [design_slot, *others])
    return ok(f"```ranking\nRANKING: {ranking}\n```\n")


def _judge_ranks_slot_order(inv):  # type: ignore[no-untyped-def]
    slots = sorted(_slots_in(inv.prompt))
    return ok("```ranking\nRANKING: " + " ".join(str(s) for s in slots) + "\n```\n")


@pytest.mark.asyncio
async def test_altitude_panel_selects_design_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bootstrap_repo(tmp_path)
    _force_structural(monkeypatch)
    adapter = StubAdapter(
        {"framing": ok(_design_text()), "altitude_judge": _judge_ranks_design_first}
    )
    orch = _make_orch(tmp_path, adapter)
    decision = await run_framing_phase(orch, "trim it", "", "", None, "abc123")
    assert decision is not None
    assert decision.classification == "realized_design_failure"
    assert decision.chosen_approach.altitude == "design_fix"
    assert adapter.count("altitude_judge") == 3


@pytest.mark.asyncio
async def test_altitude_panel_two_approaches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bootstrap_repo(tmp_path)
    _force_structural(monkeypatch)
    adapter = StubAdapter(
        {"framing": ok(_design_text()), "altitude_judge": _judge_ranks_design_first}
    )
    orch = _make_orch(tmp_path, adapter)
    orch.cfg.framing.num_approaches = 2  # valid_labels must become "12", not "123"
    decision = await run_framing_phase(orch, "trim it", "", "", None, "abc123")
    assert decision is not None
    assert decision.classification == "realized_design_failure"
    assert len(decision.approaches) == 2
    assert decision.chosen_approach.altitude == "design_fix"


@pytest.mark.asyncio
async def test_altitude_panel_partial_judge_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bootstrap_repo(tmp_path)
    _force_structural(monkeypatch)
    state = {"n": 0}

    def _judge(inv):  # type: ignore[no-untyped-def]
        state["n"] += 1
        if state["n"] == 1:
            return ok("no ranking line here")
        return _judge_ranks_design_first(inv)

    adapter = StubAdapter({"framing": ok(_design_text()), "altitude_judge": _judge})
    orch = _make_orch(tmp_path, adapter)
    decision = await run_framing_phase(orch, "trim it", "", "", None, "abc123")
    assert decision is not None
    assert decision.chosen_approach.altitude == "design_fix"


@pytest.mark.asyncio
async def test_altitude_panel_all_fail_tiebreak_local_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bootstrap_repo(tmp_path)
    _force_structural(monkeypatch)
    adapter = StubAdapter(
        {"framing": ok(_design_text()), "altitude_judge": ok("garbage no ranking line")}
    )
    orch = _make_orch(tmp_path, adapter)
    decision = await run_framing_phase(orch, "trim it", "", "", None, "abc123")
    assert decision is not None
    assert decision.chosen_approach.altitude == "local_patch"


def test_altitude_panel_order_inversion() -> None:
    import random

    from orchestrator.framing_phase import _shuffle_approaches

    order = _shuffle_approaches(["x", "y", "z"], random.Random(123))
    assert sorted(order) == [1, 2, 3]
    assert sorted(order.values()) == ["x", "y", "z"]
    slot_ranking = [3, 1, 2]
    # inverse-map slot ranking -> canonical names via THIS judge's order map
    assert [order[s] for s in slot_ranking] == [order[3], order[1], order[2]]
    assert sorted(order[s] for s in slot_ranking) == ["x", "y", "z"]


@pytest.mark.asyncio
async def test_altitude_panel_deterministic_from_spec_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_structural(monkeypatch)

    async def _run(d: Path):  # type: ignore[no-untyped-def]
        d.mkdir()
        _bootstrap_repo(d)
        adapter = StubAdapter(
            {"framing": ok(_design_text()), "altitude_judge": _judge_ranks_slot_order}
        )
        orch = _make_orch(d, adapter)
        # rank-by-slot makes the winner depend on the shuffle, so equal winners
        # across runs proves the spec_hash-seeded shuffle is deterministic.
        return await run_framing_phase(orch, "trim it", "", "", None, "deadbeef")

    dec1 = await _run(tmp_path / "a")
    dec2 = await _run(tmp_path / "b")
    assert dec1 is not None and dec2 is not None
    assert dec1.chosen_approach.name == dec2.chosen_approach.name


# --- Phase 6: merge gates ---------------------------------------------------

_REGRESSION_DIR = Path(__file__).parent / "fixtures" / "regression"
_CONSERVATISM_BUGS = sorted(
    (Path(__file__).parent / "fixtures" / "conservatism_bugs").glob("*.md")
)


@pytest.mark.asyncio
async def test_framing_phase_201_200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate 1 (decisive): the #201/#200 replay surfaces AND selects the design fix.

    Replays the vendored synaptix_core_bug.md through run_framing_phase with a
    StubAdapter returning a realized_design_failure framing + both altitudes; the
    panel selects the design_fix.
    """
    bug = (_REGRESSION_DIR / "synaptix_core_bug.md").read_text(encoding="utf-8")
    assert "429" in bug and "rate_limited" in bug  # vendored real content
    _bootstrap_repo(tmp_path)
    _force_structural(monkeypatch)
    framing_text = _framing_text("realized_design_failure", 0.85) + _approaches_text(
        _LOCAL_ITEM, _DESIGN_ITEM
    )
    adapter = StubAdapter(
        {"framing": ok(framing_text), "altitude_judge": _judge_ranks_design_first}
    )
    orch = _make_orch(tmp_path, adapter)
    decision = await run_framing_phase(orch, bug, "", "", None, "feedface")
    assert decision is not None
    assert decision.classification == "realized_design_failure"
    assert decision.confidence >= 0.7
    altitudes = {a.altitude for a in decision.approaches}
    assert "local_patch" in altitudes
    assert "design_fix" in altitudes
    assert decision.chosen_approach.altitude == "design_fix"
    ops = [e.op for e in read_entries(tmp_path)]
    assert "framing_strategy_chosen" in ops


@pytest.mark.parametrize(
    "bug_path", _CONSERVATISM_BUGS, ids=lambda p: p.name
)
@pytest.mark.asyncio
async def test_framing_conservatism_corpus(tmp_path: Path, bug_path: Path) -> None:
    """Gate 2 (false-positive gate): every known-local bug stays local_defect,
    yields a single local_patch, and fires ZERO altitude_judge calls."""
    bug = bug_path.read_text(encoding="utf-8")
    _bootstrap_repo(tmp_path)
    # The conservative classifier classifies these local; no structural signal
    # fires (candidate_digest=None), so the gate keeps them local_defect.
    adapter = StubAdapter({"framing": ok(_framing_text("local_defect", 0.3))})
    orch = _make_orch(tmp_path, adapter)
    decision = await run_framing_phase(orch, bug, "", "", None, "abc123")
    assert decision is not None
    assert decision.classification == "local_defect"
    assert len(decision.approaches) == 1
    assert decision.approaches[0].altitude == "local_patch"
    assert adapter.count("altitude_judge") == 0


def test_conservatism_corpus_is_non_empty() -> None:
    """Guard: the corpus must actually contain fixtures (else Gate 2 is vacuous)."""
    assert len(_CONSERVATISM_BUGS) >= 4
