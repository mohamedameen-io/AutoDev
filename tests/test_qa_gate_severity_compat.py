"""v0.22.0 backward-compat: severity defaults to 'block', preserves halt path.

Step 2 of Phase 1: extending :class:`plugins.registry.GateResult` with a
``severity`` field MUST NOT change the behavior of existing gates that do
not set the field. A ``GateResult(passed=False)`` (no explicit severity)
inherits the ``"block"`` default and therefore halts the orchestrator's
gate dispatcher exactly as in v0.21.0.

These tests pin that contract so a future refactor cannot silently
demote legacy gates from block to warn.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from adapters.types import AgentResult
from plugins.registry import GateResult


def test_gate_result_default_severity_is_block() -> None:
    """A bare ``GateResult(passed=...)`` defaults to severity='block'."""
    r = GateResult(passed=True)
    assert r.severity == "block"
    r2 = GateResult(passed=False, details="oops")
    assert r2.severity == "block"


def test_gate_result_default_metrics_is_empty_dict() -> None:
    """The new ``metrics`` field defaults to an empty dict (not None)."""
    r = GateResult(passed=True)
    assert r.metrics == {}
    # Mutating one instance's metrics must not affect another (default_factory).
    r.metrics["x"] = 1
    r2 = GateResult(passed=True)
    assert r2.metrics == {}


def test_gate_result_accepts_severity_keyword() -> None:
    """The new field is keyword-settable."""
    r = GateResult(passed=True, severity="warn", details="d", metrics={"k": 1})
    assert r.severity == "warn"
    assert r.metrics == {"k": 1}


@pytest.mark.asyncio
async def test_legacy_failing_gate_still_halts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stub gate returning ``GateResult(passed=False)`` (no severity set)
    must trigger the halt path in ``_run_qa_gates`` — byte-identical to
    pre-v0.22.0 behavior."""
    from orchestrator import execute_phase as ep

    async def fake_failing_secretscan(
        cwd: Path,
        paths: list[Path] | None = None,
        edit_scope: list[str] | None = None,
        per_extension_thresholds: dict[str, float] | None = None,
        baseline_enabled: bool = False,
    ) -> GateResult:
        # NO severity argument — relies on default = "block".
        return GateResult(passed=False, details="legacy halt!")

    async def fake_pass(*_a: object, **_k: object) -> GateResult:
        return GateResult(passed=True, details="ok")

    monkeypatch.setattr(ep, "run_secretscan", fake_failing_secretscan)
    monkeypatch.setattr(ep, "run_syntax_check", lambda *a, **k: fake_pass())
    monkeypatch.setattr(ep, "run_lint", lambda *a, **k: fake_pass())
    monkeypatch.setattr(ep, "run_build_check", lambda *a, **k: fake_pass())
    monkeypatch.setattr(ep, "run_tests", lambda *a, **k: fake_pass())
    monkeypatch.setattr(ep, "run_hallucination_guard", lambda *a, **k: fake_pass())
    monkeypatch.setattr(ep, "detect_language", lambda *a, **k: "python")

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
            code_size = False
            code_size_baseline_enabled = False
            code_size_thresholds = None

        hallucination_guard = False  # disable to isolate secretscan path

    orch = type(
        "OrchStub",
        (),
        {"cfg": FakeCfg(), "cwd": tmp_path, "plugin_registry": None},
    )()

    class FakeTask:
        id = "1.1"
        metadata: dict = {}

    out = await ep._run_qa_gates(orch, FakeTask(), developer_result=None)
    assert out == "legacy halt!", "legacy gate failure should halt with details"


@pytest.mark.asyncio
async def test_warn_severity_does_not_halt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stub gate returning warn-severity surfaces a warning AND lets
    dispatch continue (gate returns None ⇒ no halt)."""
    from orchestrator import execute_phase as ep

    async def fake_warn_secretscan(
        cwd: Path,
        paths: list[Path] | None = None,
        edit_scope: list[str] | None = None,
        per_extension_thresholds: dict[str, float] | None = None,
        baseline_enabled: bool = False,
    ) -> GateResult:
        return GateResult(
            passed=True,
            severity="warn",
            details="just a heads-up",
            metrics={"foo": 1},
        )

    async def fake_pass(*_a: object, **_k: object) -> GateResult:
        return GateResult(passed=True, details="ok")

    monkeypatch.setattr(ep, "run_secretscan", fake_warn_secretscan)
    monkeypatch.setattr(ep, "run_syntax_check", lambda *a, **k: fake_pass())
    monkeypatch.setattr(ep, "run_lint", lambda *a, **k: fake_pass())
    monkeypatch.setattr(ep, "run_build_check", lambda *a, **k: fake_pass())
    monkeypatch.setattr(ep, "run_tests", lambda *a, **k: fake_pass())
    monkeypatch.setattr(ep, "run_hallucination_guard", lambda *a, **k: fake_pass())
    monkeypatch.setattr(ep, "detect_language", lambda *a, **k: "python")

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
            code_size = False
            code_size_baseline_enabled = False
            code_size_thresholds = None

        hallucination_guard = False

    orch = type(
        "OrchStub",
        (),
        {"cfg": FakeCfg(), "cwd": tmp_path, "plugin_registry": None},
    )()

    class FakeTask:
        id = "1.1"
        metadata: dict = {}

    out = await ep._run_qa_gates(orch, FakeTask(), developer_result=None)
    assert out is None, "warn-severity must not halt the dispatcher"
    warnings = FakeTask.metadata.get("qa_warnings", [])
    assert len(warnings) == 1
    assert warnings[0]["gate"] == "secretscan"
    assert warnings[0]["severity"] == "warn"
    assert warnings[0]["details"] == "just a heads-up"
    assert warnings[0]["metrics"] == {"foo": 1}


@pytest.mark.asyncio
async def test_info_severity_failure_does_not_halt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """passed=False + severity='info' surfaces an info note, no halt."""
    from orchestrator import execute_phase as ep

    async def fake_info_failing(
        cwd: Path,
        paths: list[Path] | None = None,
        edit_scope: list[str] | None = None,
        per_extension_thresholds: dict[str, float] | None = None,
        baseline_enabled: bool = False,
    ) -> GateResult:
        return GateResult(
            passed=False,
            severity="info",
            details="advisory",
        )

    async def fake_pass(*_a: object, **_k: object) -> GateResult:
        return GateResult(passed=True, details="ok")

    monkeypatch.setattr(ep, "run_secretscan", fake_info_failing)
    monkeypatch.setattr(ep, "run_syntax_check", lambda *a, **k: fake_pass())
    monkeypatch.setattr(ep, "run_lint", lambda *a, **k: fake_pass())
    monkeypatch.setattr(ep, "run_build_check", lambda *a, **k: fake_pass())
    monkeypatch.setattr(ep, "run_tests", lambda *a, **k: fake_pass())
    monkeypatch.setattr(ep, "run_hallucination_guard", lambda *a, **k: fake_pass())
    monkeypatch.setattr(ep, "detect_language", lambda *a, **k: "python")

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
            code_size = False
            code_size_baseline_enabled = False
            code_size_thresholds = None

        hallucination_guard = False

    orch = type(
        "OrchStub",
        (),
        {"cfg": FakeCfg(), "cwd": tmp_path, "plugin_registry": None},
    )()

    class FakeTask:
        id = "1.1"
        metadata: dict = {}

    out = await ep._run_qa_gates(orch, FakeTask(), developer_result=None)
    assert out is None
    warnings = FakeTask.metadata.get("qa_warnings", [])
    assert len(warnings) == 1
    assert warnings[0]["severity"] == "info"
