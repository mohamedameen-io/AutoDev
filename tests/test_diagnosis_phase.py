"""run_diagnosis_phase tests (ADR-0046) — loop build, reproduce, ranked
hypotheses, seam signal, fidelity honesty, flag-guard + fail-safe, resume.

Mirrors ``tests/test_framing_phase.py`` style: a git-bootstrapped tmp repo, a
``StubAdapter`` for the unregistered ``diagnostician`` specialist role, and
ledger-op + evidence assertions.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator.diagnosis_phase import (
    DiagnosisOutcome,
    is_bug_fix,
    run_diagnosis_phase,
)
from state.evidence import read_evidence, write_evidence
from state.ledger import read_entries
from state.schemas import DiagnosisEvidence, FeedbackLoop, Hypothesis
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
        session_id="sess-diagnosis",
    )


_BUG_SPEC = "## Scope: Fix the 429 rate_limited crash when fetching an oversized observation"


def _diagnosis_text(
    *,
    method: str = "replay_trace",
    fidelity: str = "replay",
    reproduced: bool = True,
    seam: str = "correct",
    recurrence: bool = False,
    cause: str = "de-amplified retry not applied on bloated fetch",
    artifact: str | None = None,
    hypotheses: int = 3,
) -> str:
    hyp_lines = "\n".join(
        f"HYPOTHESIS {i}: cause-{i} drives the symptom || "
        f"if cause-{i}, then changing knob-{i} makes the 429 disappear"
        for i in range(1, hypotheses + 1)
    )
    return (
        "```diagnosis\n"
        f"LOOP_METHOD: {method}\n"
        "LOOP_COMMAND: uv run pytest tests/repro/test_429.py -q\n"
        f"LOOP_FIDELITY: {fidelity}\n"
        "LOOP_DETERMINISTIC: true\n"
        f"REPRODUCED: {'true' if reproduced else 'false'}\n"
        "SYMPTOM: 429 rate_limited after the oversized observation fetch\n"
        f"{hyp_lines}\n"
        f"CONFIRMED_CAUSE: {cause}\n"
        f"SEAM: {seam}\n"
        f"RECURRENCE_AT_SEAM: {'true' if recurrence else 'false'}\n"
        f"LIVE_REPRO_ARTIFACT: {artifact if artifact is not None else 'none'}\n"
        "```\n"
    )


# --- is-bug-fix gate --------------------------------------------------------


def test_is_bug_fix_detects_bug() -> None:
    assert is_bug_fix("## Scope: Fix the crash that happens on startup")
    assert is_bug_fix("There is a regression: the parser fails on empty input")
    assert is_bug_fix("Bug: incorrect total when the cart is empty")


def test_is_bug_fix_rejects_feature() -> None:
    assert not is_bug_fix(
        "## Scope: Add a new dashboard widget to display per-user metrics"
    )
    assert not is_bug_fix("Implement a CSV export endpoint for the reports page")
    assert not is_bug_fix("")


# --- flag-guards ------------------------------------------------------------


@pytest.mark.asyncio
async def test_diagnosis_disabled_via_config(tmp_path: Path) -> None:
    _bootstrap_repo(tmp_path)
    adapter = StubAdapter({})
    orch = _make_orch(tmp_path, adapter)
    orch.cfg.diagnosis.enabled = False
    outcome = await run_diagnosis_phase(orch, _BUG_SPEC, "")
    assert outcome.reason == "disabled"
    assert outcome.seam == "unknown"
    assert adapter.count("diagnostician") == 0


@pytest.mark.asyncio
async def test_diagnosis_disabled_via_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bootstrap_repo(tmp_path)
    monkeypatch.setenv("AUTODEV_DIAGNOSIS_DISABLED", "1")
    adapter = StubAdapter({})
    orch = _make_orch(tmp_path, adapter)
    outcome = await run_diagnosis_phase(orch, _BUG_SPEC, "")
    assert outcome.reason == "disabled"
    assert adapter.count("diagnostician") == 0


@pytest.mark.asyncio
async def test_diagnosis_skips_feature_work(tmp_path: Path) -> None:
    _bootstrap_repo(tmp_path)
    adapter = StubAdapter({})
    orch = _make_orch(tmp_path, adapter)
    outcome = await run_diagnosis_phase(
        orch, "## Scope: Add a new export endpoint for analytics dashboards", ""
    )
    assert outcome.reason == "not_bug_fix"
    assert adapter.count("diagnostician") == 0
    assert not outcome.ran


# --- offline loop build + reproduce + ranked hypotheses ---------------------


@pytest.mark.asyncio
async def test_offline_loop_build_reproduce_and_hypotheses(tmp_path: Path) -> None:
    _bootstrap_repo(tmp_path)
    adapter = StubAdapter({"diagnostician": ok(_diagnosis_text(hypotheses=3))})
    orch = _make_orch(tmp_path, adapter)
    outcome = await run_diagnosis_phase(orch, _BUG_SPEC, "explorer says: retry.py")
    assert adapter.count("diagnostician") == 1
    assert outcome.reproduced is True
    assert outcome.seam == "correct"
    assert outcome.confirmed_cause is not None
    ev = await read_evidence(tmp_path, "plan-diagnosis", "diagnosis")
    assert isinstance(ev, DiagnosisEvidence)
    assert len(ev.hypotheses) == 3
    assert all(h.prediction for h in ev.hypotheses)  # falsifiable
    assert ev.loop is not None
    assert ev.loop.method == "replay_trace"
    assert (
        tmp_path / ".autodev" / "evidence" / "plan-diagnosis-diagnosis.json"
    ).exists()


@pytest.mark.asyncio
async def test_diagnosis_dispatch_uses_specialist_path_not_registry(
    tmp_path: Path,
) -> None:
    _bootstrap_repo(tmp_path)
    adapter = StubAdapter({"diagnostician": ok(_diagnosis_text())})
    orch = _make_orch(tmp_path, adapter)
    # diagnostician is deliberately NOT in the registry (build_registry only
    # iterates REQUIRED_AGENT_ROLES); dispatch must bypass it via load_prompt.
    assert "diagnostician" not in orch.registry
    await run_diagnosis_phase(orch, _BUG_SPEC, "")
    assert adapter.count("diagnostician") == 1
    assert any(c.role == "diagnostician" for c in adapter.calls)


def test_vibe_hypotheses_without_prediction_rejected() -> None:
    from orchestrator.diagnosis_phase import _parse_hypotheses

    text = (
        "HYPOTHESIS 1: it is probably the retry logic\n"  # no `||` prediction
        "HYPOTHESIS 2: the cache is stale || if stale, clearing it fixes it\n"
    )
    hyps, diags = _parse_hypotheses(text, max_hypotheses=5)
    assert len(hyps) == 1
    assert hyps[0].rank == 2
    assert any("no prediction" in d for d in diags)


# --- seam signal routed to framing ------------------------------------------


@pytest.mark.asyncio
async def test_no_correct_seam_emits_structural_signal(tmp_path: Path) -> None:
    """SEAM: none ⇒ no_correct_seam structural signal (fed to framing)."""
    _bootstrap_repo(tmp_path)
    adapter = StubAdapter({"diagnostician": ok(_diagnosis_text(seam="none"))})
    orch = _make_orch(tmp_path, adapter)
    outcome = await run_diagnosis_phase(orch, _BUG_SPEC, "")
    assert outcome.seam == "none"
    assert outcome.no_correct_seam is True
    assert "no_correct_seam" in outcome.structural_signals
    ev = await read_evidence(tmp_path, "plan-diagnosis", "diagnosis")
    assert isinstance(ev, DiagnosisEvidence)
    assert ev.no_correct_seam is True
    ops = [e.op for e in read_entries(tmp_path)]
    assert "seam_finding" in ops


@pytest.mark.asyncio
async def test_shallow_seam_also_signals_no_correct_seam(tmp_path: Path) -> None:
    _bootstrap_repo(tmp_path)
    adapter = StubAdapter({"diagnostician": ok(_diagnosis_text(seam="shallow"))})
    orch = _make_orch(tmp_path, adapter)
    outcome = await run_diagnosis_phase(orch, _BUG_SPEC, "")
    assert outcome.seam == "shallow"
    assert outcome.no_correct_seam is True


@pytest.mark.asyncio
async def test_recurrence_at_seam_threaded_through(tmp_path: Path) -> None:
    _bootstrap_repo(tmp_path)
    adapter = StubAdapter(
        {"diagnostician": ok(_diagnosis_text(seam="correct", recurrence=True))}
    )
    orch = _make_orch(tmp_path, adapter)
    outcome = await run_diagnosis_phase(orch, _BUG_SPEC, "")
    assert outcome.recurrence_at_seam is True
    assert "recurrence_at_seam" in outcome.structural_signals


# --- ledger ops -------------------------------------------------------------


@pytest.mark.asyncio
async def test_diagnosis_emits_ledger_ops(tmp_path: Path) -> None:
    _bootstrap_repo(tmp_path)
    adapter = StubAdapter({"diagnostician": ok(_diagnosis_text(method="failing_test", fidelity="synthetic"))})
    orch = _make_orch(tmp_path, adapter)
    await run_diagnosis_phase(orch, _BUG_SPEC, "")
    ops = [e.op for e in read_entries(tmp_path)]
    assert "diagnosis_loop_built" in ops
    assert "bug_reproduced" in ops
    assert "hypotheses_ranked" in ops
    assert "cause_confirmed" in ops
    assert "seam_finding" in ops


# --- fidelity honesty (NFR5): never report `live` on a network-less run -----


@pytest.mark.asyncio
async def test_loop_fidelity_never_live_on_networkless_run(tmp_path: Path) -> None:
    """A diagnostician that (wrongly) claims LOOP_FIDELITY: live is downgraded
    to synthetic — the autonomous run has no network/creds (NFR5)."""
    _bootstrap_repo(tmp_path)
    adapter = StubAdapter(
        {"diagnostician": ok(_diagnosis_text(fidelity="live", method="replay_trace"))}
    )
    orch = _make_orch(tmp_path, adapter)
    outcome = await run_diagnosis_phase(orch, _BUG_SPEC, "")
    assert outcome.loop_fidelity != "live"
    assert outcome.loop_fidelity == "synthetic"
    ev = await read_evidence(tmp_path, "plan-diagnosis", "diagnosis")
    assert isinstance(ev, DiagnosisEvidence)
    assert ev.loop_fidelity == "synthetic"
    assert ev.loop is not None
    assert ev.loop.fidelity == "synthetic"


@pytest.mark.asyncio
async def test_live_method_becomes_synthetic_loop_with_artifact(tmp_path: Path) -> None:
    """A live method (dev_server_curl) with a delivered artifact ⇒ synthetic
    fidelity + repro_unavailable_live ledger op (the §5.2 fallback)."""
    _bootstrap_repo(tmp_path)
    adapter = StubAdapter(
        {
            "diagnostician": ok(
                _diagnosis_text(
                    method="dev_server_curl",
                    fidelity="live",
                    artifact="scripts/repro/mistral_429.sh",
                )
            )
        }
    )
    orch = _make_orch(tmp_path, adapter)
    outcome = await run_diagnosis_phase(orch, _BUG_SPEC, "")
    assert outcome.loop_fidelity == "synthetic"
    ev = await read_evidence(tmp_path, "plan-diagnosis", "diagnosis")
    assert isinstance(ev, DiagnosisEvidence)
    assert ev.live_repro_artifact == "scripts/repro/mistral_429.sh"
    ops = [e.op for e in read_entries(tmp_path)]
    assert "repro_unavailable_live" in ops


# --- fail-safe: a raising adapter degrades, planning continues --------------


@pytest.mark.asyncio
async def test_fail_safe_on_raising_adapter(tmp_path: Path) -> None:
    _bootstrap_repo(tmp_path)

    def _boom(inv):  # type: ignore[no-untyped-def]
        raise RuntimeError("adapter exploded")

    adapter = StubAdapter({"diagnostician": _boom})
    orch = _make_orch(tmp_path, adapter)
    # Must NOT raise — degrades to a pass-through so planning continues.
    outcome = await run_diagnosis_phase(orch, _BUG_SPEC, "")
    assert isinstance(outcome, DiagnosisOutcome)
    assert outcome.reason == "dispatch_error"
    assert outcome.seam == "unknown"
    assert outcome.reproduced is False


@pytest.mark.asyncio
async def test_fail_safe_on_garbage_response(tmp_path: Path) -> None:
    """A garbage (unparseable) diagnostician response degrades cleanly: no loop,
    unknown seam, but evidence is still written and planning continues."""
    _bootstrap_repo(tmp_path)
    adapter = StubAdapter({"diagnostician": ok("no structured block here at all")})
    orch = _make_orch(tmp_path, adapter)
    outcome = await run_diagnosis_phase(orch, _BUG_SPEC, "")
    assert outcome.reason == "ok"  # ran + parsed (skeptically), did not error
    assert outcome.seam == "unknown"
    assert outcome.reproduced is False
    assert outcome.loop_fidelity == "none"
    ev = await read_evidence(tmp_path, "plan-diagnosis", "diagnosis")
    assert isinstance(ev, DiagnosisEvidence)
    assert ev.loop is None


# --- resume re-reads evidence (0 dispatches) --------------------------------


@pytest.mark.asyncio
async def test_resume_rereads_evidence_zero_dispatches(tmp_path: Path) -> None:
    _bootstrap_repo(tmp_path)
    ev = DiagnosisEvidence(
        task_id="plan-diagnosis",
        loop=FeedbackLoop(
            method="replay_trace",
            command="uv run pytest -q",
            fidelity="replay",
            deterministic=True,
        ),
        reproduced=True,
        symptom="429 after bloated fetch",
        hypotheses=[
            Hypothesis(rank=1, statement="s", prediction="if s then y")
        ],
        confirmed_cause="de-amplified retry not applied",
        seam="none",
        loop_fidelity="replay",
        recurrence_at_seam=True,
        no_correct_seam=True,
    )
    await write_evidence(tmp_path, "plan-diagnosis", ev)
    adapter = StubAdapter({})  # empty: any dispatch would be a fallback
    orch = _make_orch(tmp_path, adapter)
    outcome = await run_diagnosis_phase(orch, _BUG_SPEC, "")
    assert adapter.count("diagnostician") == 0
    assert outcome.reason == "ok"
    assert outcome.seam == "none"
    assert outcome.no_correct_seam is True
    assert outcome.recurrence_at_seam is True
    assert outcome.confirmed_cause == "de-amplified retry not applied"
    assert "no_correct_seam" in outcome.structural_signals
    assert "recurrence_at_seam" in outcome.structural_signals
