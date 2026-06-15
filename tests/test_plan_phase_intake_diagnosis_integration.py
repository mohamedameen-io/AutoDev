"""Integration wiring tests for intake (ADR-0045) + diagnosis (ADR-0046).

Workstream D: ``run_plan_phase`` inserts ``run_intake_phase`` (rebinding the
local ``intent``/``spec_hash`` to the LOCKED ENRICHED SPEC) and
``run_diagnosis_phase`` (threaded into framing as ``diagnosis_signals`` and into
the architect context) between the domain_expert evidence write and framing.

Both phases are flag-guarded + fail-safe — neither may ever block planning.

Mirrors ``test_orchestrator_plan_phase.py`` (StubAdapter + ``_make_orch``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator import plan_phase as plan_phase_mod

from stub_adapter import StubAdapter, ok


CANONICAL_PLAN_MD = """
# Plan: Fix the bug

## Phase 1: Implement

### Task 1.1: Patch the defect in math.py
  - Description: Fix subtract regression
  - Files: math.py
  - Acceptance:
    - [ ] Returns correct value
"""


# --- Lightweight stand-ins for IntakeOutcome / DiagnosisOutcome ------------
# We monkeypatch the phase entry points the plan phase imported, so we only
# need duck-typed objects with the fields the integration reads.


@dataclass
class _StubIntakeOutcome:
    spec: str
    spec_hash: str
    assumptions: list[str] = field(default_factory=list)
    degraded: bool = False
    passthrough: bool = False


@dataclass
class _StubDiagnosisOutcome:
    confirmed_cause: str | None
    seam: Literal["correct", "shallow", "none", "unknown"]
    reproduced: bool = False
    loop_fidelity: str = "none"
    recurrence_at_seam: bool = False
    no_correct_seam: bool = False
    reason: str = "ok"
    structural_signals: list[str] = field(default_factory=list)

    @property
    def ran(self) -> bool:
        return self.reason not in ("disabled", "not_bug_fix")


def _make_orch(cwd: Path, adapter: StubAdapter) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    return Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=build_registry(cfg),
        session_id="sess-intake-diag",
    )


def _adapter() -> StubAdapter:
    return StubAdapter(
        {
            "explorer": ok("explored: math.py regression in subtract"),
            "domain_expert": ok("no special domain considerations"),
            "architect": ok(CANONICAL_PLAN_MD),
        }
    )


def _capture_framing(monkeypatch: pytest.MonkeyPatch, sink: dict) -> None:
    """Spy on run_framing_phase: record kwargs, return None (architect default)."""

    async def _fake_framing(**kwargs: object) -> None:
        sink["framing_kwargs"] = kwargs
        return None

    monkeypatch.setattr(plan_phase_mod, "run_framing_phase", _fake_framing)


def _capture_architect_context(
    monkeypatch: pytest.MonkeyPatch, sink: dict
) -> None:
    """Spy on _delegate to capture the architect envelope's context dict."""
    real_delegate = plan_phase_mod._delegate

    async def _spy(
        orch: object, role: str, env: object, *args: object, **kwargs: object
    ) -> object:
        if role == "architect":
            sink["architect_context"] = dict(getattr(env, "context", {}))
        return await real_delegate(orch, role, env, *args, **kwargs)

    monkeypatch.setattr(plan_phase_mod, "_delegate", _spy)


# --- Intake: enriched spec rebind ------------------------------------------


@pytest.mark.asyncio
async def test_intake_enriched_spec_rebound_into_framing_and_architect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An enriched IntakeOutcome rebinds ``intent`` + ``spec_hash`` for every
    downstream consumer (framing intent=/spec_hash=, architect context["spec"])."""
    sink: dict = {}

    async def _fake_intake(orch: object, intent: str, **_k: object):
        return _StubIntakeOutcome(
            spec="ENRICHED: " + intent, spec_hash="enriched-hash-123"
        )

    async def _fake_diagnosis(orch: object, spec: str, explore_ev: str):
        return _StubDiagnosisOutcome(confirmed_cause=None, seam="unknown")

    monkeypatch.setattr(plan_phase_mod, "run_intake_phase", _fake_intake)
    monkeypatch.setattr(plan_phase_mod, "run_diagnosis_phase", _fake_diagnosis)
    _capture_framing(monkeypatch, sink)
    _capture_architect_context(monkeypatch, sink)

    orch = _make_orch(tmp_path, _adapter())
    await orch.plan("Fix subtract bug")

    fk = sink["framing_kwargs"]
    assert fk["intent"] == "ENRICHED: Fix subtract bug"
    assert fk["spec_hash"] == "enriched-hash-123"
    assert sink["architect_context"]["spec"] == "ENRICHED: Fix subtract bug"


@pytest.mark.asyncio
async def test_intake_disabled_via_env_keeps_raw_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AUTODEV_INTAKE_DISABLED → run_intake_phase is never called; raw intent
    flows unchanged into framing + architect."""
    sink: dict = {}
    monkeypatch.setenv("AUTODEV_INTAKE_DISABLED", "1")

    called = {"intake": False}

    async def _fake_intake(orch: object, intent: str, **_k: object):
        called["intake"] = True
        return _StubIntakeOutcome(spec="SHOULD-NOT-APPEAR", spec_hash="x")

    async def _fake_diagnosis(orch: object, spec: str, explore_ev: str):
        return _StubDiagnosisOutcome(confirmed_cause=None, seam="unknown")

    monkeypatch.setattr(plan_phase_mod, "run_intake_phase", _fake_intake)
    monkeypatch.setattr(plan_phase_mod, "run_diagnosis_phase", _fake_diagnosis)
    _capture_framing(monkeypatch, sink)
    _capture_architect_context(monkeypatch, sink)

    orch = _make_orch(tmp_path, _adapter())
    await orch.plan("Fix subtract bug")

    assert called["intake"] is False
    assert sink["framing_kwargs"]["intent"] == "Fix subtract bug"
    assert sink["architect_context"]["spec"] == "Fix subtract bug"


@pytest.mark.asyncio
async def test_intake_disabled_via_config_keeps_raw_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cfg.intake.enabled=False → raw intent flows unchanged."""
    sink: dict = {}

    async def _fake_intake(orch: object, intent: str, **_k: object):
        raise AssertionError("run_intake_phase must not be called when disabled")

    async def _fake_diagnosis(orch: object, spec: str, explore_ev: str):
        return _StubDiagnosisOutcome(confirmed_cause=None, seam="unknown")

    monkeypatch.setattr(plan_phase_mod, "run_intake_phase", _fake_intake)
    monkeypatch.setattr(plan_phase_mod, "run_diagnosis_phase", _fake_diagnosis)
    _capture_framing(monkeypatch, sink)
    _capture_architect_context(monkeypatch, sink)

    orch = _make_orch(tmp_path, _adapter())
    orch.cfg.intake.enabled = False
    await orch.plan("Fix subtract bug")

    assert sink["framing_kwargs"]["intent"] == "Fix subtract bug"
    assert sink["architect_context"]["spec"] == "Fix subtract bug"


@pytest.mark.asyncio
async def test_intake_raising_does_not_block_planning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raising run_intake_phase is caught — planning continues on raw intent."""
    sink: dict = {}

    async def _boom(orch: object, intent: str, **_k: object):
        raise RuntimeError("intake exploded")

    async def _fake_diagnosis(orch: object, spec: str, explore_ev: str):
        return _StubDiagnosisOutcome(confirmed_cause=None, seam="unknown")

    monkeypatch.setattr(plan_phase_mod, "run_intake_phase", _boom)
    monkeypatch.setattr(plan_phase_mod, "run_diagnosis_phase", _fake_diagnosis)
    _capture_framing(monkeypatch, sink)
    _capture_architect_context(monkeypatch, sink)

    orch = _make_orch(tmp_path, _adapter())
    plan = await orch.plan("Fix subtract bug")

    assert plan is not None
    assert sink["framing_kwargs"]["intent"] == "Fix subtract bug"


# --- Diagnosis: threaded into framing + architect context ------------------


@pytest.mark.asyncio
async def test_diagnosis_threaded_into_framing_and_architect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DiagnosisOutcome is passed to framing as ``diagnosis_signals`` and its
    confirmed_cause + seam land in the architect context."""
    sink: dict = {}
    dx = _StubDiagnosisOutcome(
        confirmed_cause="off-by-one in subtract", seam="none"
    )

    async def _fake_intake(orch: object, intent: str, **_k: object):
        return _StubIntakeOutcome(spec=intent, spec_hash="h", passthrough=True)

    async def _fake_diagnosis(orch: object, spec: str, explore_ev: str):
        return dx

    monkeypatch.setattr(plan_phase_mod, "run_intake_phase", _fake_intake)
    monkeypatch.setattr(plan_phase_mod, "run_diagnosis_phase", _fake_diagnosis)
    _capture_framing(monkeypatch, sink)
    _capture_architect_context(monkeypatch, sink)

    orch = _make_orch(tmp_path, _adapter())
    await orch.plan("Fix subtract bug")

    assert sink["framing_kwargs"]["diagnosis_signals"] is dx
    ctx = sink["architect_context"]
    assert ctx["diagnosed_cause"] == "off-by-one in subtract"
    assert ctx["diagnosis_seam"] == "none"


@pytest.mark.asyncio
async def test_diagnosis_not_run_yields_empty_architect_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A degraded (reason='not_bug_fix') outcome → ``.ran`` is False → architect
    context falls back to ""/"unknown"."""
    sink: dict = {}

    async def _fake_intake(orch: object, intent: str, **_k: object):
        return _StubIntakeOutcome(spec=intent, spec_hash="h", passthrough=True)

    async def _fake_diagnosis(orch: object, spec: str, explore_ev: str):
        return _StubDiagnosisOutcome(
            confirmed_cause="ignored", seam="none", reason="not_bug_fix"
        )

    monkeypatch.setattr(plan_phase_mod, "run_intake_phase", _fake_intake)
    monkeypatch.setattr(plan_phase_mod, "run_diagnosis_phase", _fake_diagnosis)
    _capture_framing(monkeypatch, sink)
    _capture_architect_context(monkeypatch, sink)

    orch = _make_orch(tmp_path, _adapter())
    await orch.plan("Add a new feature")

    ctx = sink["architect_context"]
    assert ctx["diagnosed_cause"] == ""
    assert ctx["diagnosis_seam"] == "unknown"


@pytest.mark.asyncio
async def test_diagnosis_raising_does_not_block_planning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raising run_diagnosis_phase is caught; diagnosis_outcome stays None and
    planning continues (framing gets diagnosis_signals=None)."""
    sink: dict = {}

    async def _fake_intake(orch: object, intent: str, **_k: object):
        return _StubIntakeOutcome(spec=intent, spec_hash="h", passthrough=True)

    async def _boom(orch: object, spec: str, explore_ev: str):
        raise RuntimeError("diagnosis exploded")

    monkeypatch.setattr(plan_phase_mod, "run_intake_phase", _fake_intake)
    monkeypatch.setattr(plan_phase_mod, "run_diagnosis_phase", _boom)
    _capture_framing(monkeypatch, sink)
    _capture_architect_context(monkeypatch, sink)

    orch = _make_orch(tmp_path, _adapter())
    plan = await orch.plan("Fix subtract bug")

    assert plan is not None
    assert sink["framing_kwargs"]["diagnosis_signals"] is None
    ctx = sink["architect_context"]
    assert ctx["diagnosed_cause"] == ""
    assert ctx["diagnosis_seam"] == "unknown"


@pytest.mark.asyncio
async def test_diagnosis_disabled_via_env_threads_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AUTODEV_DIAGNOSIS_DISABLED → run_diagnosis_phase never called; framing
    gets diagnosis_signals=None and architect context defaults."""
    sink: dict = {}
    monkeypatch.setenv("AUTODEV_DIAGNOSIS_DISABLED", "1")

    async def _fake_intake(orch: object, intent: str, **_k: object):
        return _StubIntakeOutcome(spec=intent, spec_hash="h", passthrough=True)

    async def _fake_diagnosis(orch: object, spec: str, explore_ev: str):
        raise AssertionError("diagnosis must not run when disabled")

    monkeypatch.setattr(plan_phase_mod, "run_intake_phase", _fake_intake)
    monkeypatch.setattr(plan_phase_mod, "run_diagnosis_phase", _fake_diagnosis)
    _capture_framing(monkeypatch, sink)
    _capture_architect_context(monkeypatch, sink)

    orch = _make_orch(tmp_path, _adapter())
    await orch.plan("Fix subtract bug")

    assert sink["framing_kwargs"]["diagnosis_signals"] is None
    assert sink["architect_context"]["diagnosis_seam"] == "unknown"
