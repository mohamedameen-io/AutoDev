"""Tests for v0.16.0 hallucination_guard wiring at the gate site.

Mirrors :mod:`tests.test_orchestrator_execute_phase_secretscan` — the
orchestrator's ``_run_qa_gates`` invokes ``run_hallucination_guard``
with the developer's diff paths so the guard scans only the files the
executor just modified. The guard is gated by
``cfg.qa_gates.hallucination_guard`` (default ``True``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from adapters.types import AgentResult
from plugins.registry import GateResult


async def _fake_pass(*_a: object, **_k: object) -> GateResult:
    return GateResult(passed=True, details="ok")


def _stub_orch(tmp_path: Path, *, hallucination_guard: bool = True) -> object:
    class FakeCfg:
        class qa_gates:
            syntax_check = True
            lint = True
            build_check = True
            test_runner = True
            secretscan = True
            secretscan_baseline_enabled = False
            secretscan_per_extension_thresholds = None
            mutation_test_enabled = False
            mutation_test_threshold = 0.7
            lint_timeout_s = 120.0
            test_timeout_s = 600.0

        # Top-level toggle (read off cfg.hallucination_guard, mirrors
        # the existing pattern for `cfg.qa_gates.*`).

    cfg = FakeCfg()
    cfg.hallucination_guard = hallucination_guard
    return type(
        "OrchStub",
        (),
        {
            "cfg": cfg,
            "cwd": tmp_path,
            "plugin_registry": None,
        },
    )()


@pytest.mark.asyncio
async def test_hallucination_guard_invoked_with_developer_diff_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a developer result is available, the guard is invoked with
    ``paths=[diff paths]``."""
    captured: dict = {}

    async def fake_guard(cwd: Path, paths: list[Path] | None = None, **_kw: object) -> GateResult:
        captured["cwd"] = cwd
        captured["paths"] = paths
        return GateResult(passed=True, details="ok")

    from orchestrator import execute_phase as ep

    monkeypatch.setattr(ep, "run_hallucination_guard", fake_guard)
    monkeypatch.setattr(ep, "run_secretscan", _fake_pass)
    monkeypatch.setattr(ep, "run_syntax_check", lambda *a, **k: _fake_pass())
    monkeypatch.setattr(ep, "run_lint", lambda *a, **k: _fake_pass())
    monkeypatch.setattr(ep, "run_build_check", lambda *a, **k: _fake_pass())
    monkeypatch.setattr(ep, "run_tests", lambda *a, **k: _fake_pass())
    monkeypatch.setattr(ep, "detect_language", lambda *a, **k: "python")

    orch = _stub_orch(tmp_path, hallucination_guard=True)

    class FakeTask:
        id = "1.1"

    developer_result = AgentResult(
        text="ok",
        success=True,
        duration_s=0.1,
        diff="+++ b/file_a.py\n+++ b/file_b.py\n",
    )

    out = await ep._run_qa_gates(orch, FakeTask(), developer_result=developer_result)
    assert out is None
    assert captured["cwd"] == tmp_path
    assert captured["paths"] == [Path("file_a.py"), Path("file_b.py")]


@pytest.mark.asyncio
async def test_hallucination_guard_skipped_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``cfg.hallucination_guard=False`` short-circuits the guard."""
    invoked: dict = {"called": False}

    async def fake_guard(cwd: Path, paths: list[Path] | None = None, **_kw: object) -> GateResult:
        invoked["called"] = True
        return GateResult(passed=True, details="ok")

    from orchestrator import execute_phase as ep

    monkeypatch.setattr(ep, "run_hallucination_guard", fake_guard)
    monkeypatch.setattr(ep, "run_secretscan", _fake_pass)
    monkeypatch.setattr(ep, "run_syntax_check", lambda *a, **k: _fake_pass())
    monkeypatch.setattr(ep, "run_lint", lambda *a, **k: _fake_pass())
    monkeypatch.setattr(ep, "run_build_check", lambda *a, **k: _fake_pass())
    monkeypatch.setattr(ep, "run_tests", lambda *a, **k: _fake_pass())
    monkeypatch.setattr(ep, "detect_language", lambda *a, **k: "python")

    orch = _stub_orch(tmp_path, hallucination_guard=False)

    class FakeTask:
        id = "1.1"

    await ep._run_qa_gates(orch, FakeTask(), developer_result=None)
    assert invoked["called"] is False


@pytest.mark.asyncio
async def test_hallucination_guard_failure_returns_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed guard run surfaces its details as the gate failure reason."""
    async def fake_guard(cwd: Path, paths: list[Path] | None = None, **_kw: object) -> GateResult:
        return GateResult(
            passed=False,
            details="potential hallucinated API references:\nbad.py:1: hallucinated reference — fake not found in os",
        )

    from orchestrator import execute_phase as ep

    monkeypatch.setattr(ep, "run_hallucination_guard", fake_guard)
    monkeypatch.setattr(ep, "run_secretscan", _fake_pass)
    monkeypatch.setattr(ep, "run_syntax_check", lambda *a, **k: _fake_pass())
    monkeypatch.setattr(ep, "run_lint", lambda *a, **k: _fake_pass())
    monkeypatch.setattr(ep, "run_build_check", lambda *a, **k: _fake_pass())
    monkeypatch.setattr(ep, "run_tests", lambda *a, **k: _fake_pass())
    monkeypatch.setattr(ep, "detect_language", lambda *a, **k: "python")

    orch = _stub_orch(tmp_path, hallucination_guard=True)

    class FakeTask:
        id = "1.1"

    out = await ep._run_qa_gates(orch, FakeTask(), developer_result=None)
    assert out is not None
    assert "hallucinated reference" in out
    assert "fake not found in os" in out
