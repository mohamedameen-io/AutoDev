"""ADR-0046 QA-gate wiring in ``_run_qa_gates`` (Workstream D, EDIT 3).

``run_reproduce_gate`` + ``run_debug_tag_gate`` are registered as
``(name, enabled, callable)`` triples, gated on ``cfg.diagnosis.enabled``,
fed the same diff-scoped ``secretscan_paths`` the other gates receive. Both
return :class:`GateResult` so the existing severity-dispatch loop handles them.

Mirrors ``test_orchestrator_execute_phase_secretscan.py`` (FakeCfg + FakeTask
+ monkeypatched gate functions on the ``execute_phase`` module).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from adapters.types import AgentResult
from plugins.registry import GateResult


def _pass(*_a: object, **_k: object):
    async def _coro() -> GateResult:
        return GateResult(passed=True, details="ok")

    return _coro()


class _QAGates:
    syntax_check = True
    lint = True
    build_check = True
    test_runner = True
    secretscan = False
    secretscan_baseline_enabled = False
    secretscan_per_extension_thresholds = None
    mutation_test_enabled = False
    mutation_test_threshold = 0.7
    code_size = False
    lint_timeout_s = 120.0
    test_timeout_s = 600.0
    build_check_timeout_s = 120.0  # WS2-11: build-gate timeout knob


class _Diagnosis:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled


class _Cfg:
    def __init__(self, diagnosis_enabled: bool) -> None:
        self.qa_gates = _QAGates()
        self.diagnosis = _Diagnosis(diagnosis_enabled)
        self.hallucination_guard = False


class _Task:
    id = "1.1"
    produces_diff = True


def _orch(cwd: Path, diagnosis_enabled: bool) -> object:
    return type(
        "OrchStub",
        (),
        {"cfg": _Cfg(diagnosis_enabled), "cwd": cwd, "plugin_registry": None},
    )()


def _stub_baseline_gates(monkeypatch: pytest.MonkeyPatch, ep: object) -> None:
    """No-op the built-in gates so only the diagnosis gates are observable."""
    monkeypatch.setattr(ep, "run_syntax_check", lambda *a, **k: _pass())
    monkeypatch.setattr(ep, "run_lint", lambda *a, **k: _pass())
    monkeypatch.setattr(ep, "run_build_check", lambda *a, **k: _pass())
    monkeypatch.setattr(ep, "run_tests", lambda *a, **k: _pass())
    monkeypatch.setattr(ep, "detect_language", lambda *a, **k: "python")


_DEV_RESULT = AgentResult(
    text="ok",
    success=True,
    duration_s=0.1,
    diff="+++ b/file_a.py\n+++ b/file_b.py\n",
)


@pytest.mark.asyncio
async def test_diagnosis_gates_invoked_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both gates run with the diff-scoped paths when cfg.diagnosis.enabled."""
    from orchestrator import execute_phase as ep

    captured: dict = {}

    async def fake_reproduce(cwd: Path, paths=None, **_k: object) -> GateResult:
        captured["reproduce_paths"] = paths
        return GateResult(passed=True, details="soft pass: no loop")

    async def fake_debug(cwd: Path, paths=None, *a: object, **k: object) -> GateResult:
        captured["debug_paths"] = paths
        return GateResult(passed=True, details="no tags")

    _stub_baseline_gates(monkeypatch, ep)
    monkeypatch.setattr(ep, "run_reproduce_gate", fake_reproduce)
    monkeypatch.setattr(ep, "run_debug_tag_gate", fake_debug)

    out = await ep._run_qa_gates(
        _orch(tmp_path, diagnosis_enabled=True), _Task(), developer_result=_DEV_RESULT
    )
    assert out is None
    assert captured["reproduce_paths"] == [Path("file_a.py"), Path("file_b.py")]
    assert captured["debug_paths"] == [Path("file_a.py"), Path("file_b.py")]


@pytest.mark.asyncio
async def test_diagnosis_gates_skipped_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither gate runs when cfg.diagnosis.enabled is False."""
    from orchestrator import execute_phase as ep

    called = {"reproduce": False, "debug": False}

    async def fake_reproduce(*a: object, **k: object) -> GateResult:
        called["reproduce"] = True
        return GateResult(passed=True, details="ok")

    async def fake_debug(*a: object, **k: object) -> GateResult:
        called["debug"] = True
        return GateResult(passed=True, details="ok")

    _stub_baseline_gates(monkeypatch, ep)
    monkeypatch.setattr(ep, "run_reproduce_gate", fake_reproduce)
    monkeypatch.setattr(ep, "run_debug_tag_gate", fake_debug)

    out = await ep._run_qa_gates(
        _orch(tmp_path, diagnosis_enabled=False), _Task(), developer_result=_DEV_RESULT
    )
    assert out is None
    assert called == {"reproduce": False, "debug": False}


@pytest.mark.asyncio
async def test_debug_tag_blocks_on_leftover_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leftover [DEBUG-...] marker in a changed file blocks the gate set
    (real run_debug_tag_gate, not stubbed)."""
    from orchestrator import execute_phase as ep

    (tmp_path / "file_a.py").write_text(
        "x = 1  # [DEBUG-TRACE] remove me before merge\n"
    )

    async def fake_reproduce(*a: object, **k: object) -> GateResult:
        return GateResult(passed=True, details="soft pass")

    _stub_baseline_gates(monkeypatch, ep)
    monkeypatch.setattr(ep, "run_reproduce_gate", fake_reproduce)
    # run_debug_tag_gate is the REAL one — it should find the tag and block.

    out = await ep._run_qa_gates(
        _orch(tmp_path, diagnosis_enabled=True), _Task(), developer_result=_DEV_RESULT
    )
    assert out is not None  # blocking failure detail string
    assert "DEBUG" in out or "debug" in out.lower()


@pytest.mark.asyncio
async def test_reproduce_gate_soft_passes_without_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent a persisted diagnosis loop, the REAL reproduce_gate soft-passes
    (info severity) and does NOT block the gate set."""
    from orchestrator import execute_phase as ep

    (tmp_path / "file_a.py").write_text("x = 1\n")
    (tmp_path / "file_b.py").write_text("y = 2\n")

    _stub_baseline_gates(monkeypatch, ep)
    # run_reproduce_gate AND run_debug_tag_gate are REAL: no loop persisted →
    # reproduce soft-passes; no debug tags → debug passes.

    out = await ep._run_qa_gates(
        _orch(tmp_path, diagnosis_enabled=True), _Task(), developer_result=_DEV_RESULT
    )
    assert out is None  # nothing blocked
