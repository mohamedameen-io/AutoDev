"""Phase 1 orchestrator integration: code_size gate is dispatched and warnings
surface on the task's metadata bundle without halting the dispatcher.

Mirrors the shape of ``test_orchestrator_execute_phase_secretscan.py`` —
stubs every other gate to a clean pass and observes only the code_size
path. Uses the verbose pair_01 fixture to guarantee the gate's warn path
fires under the strict thresholds we set up here.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from adapters.types import AgentResult
from plugins.registry import GateResult
from qa.code_size import run_code_size


_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "anti_bloat"


@pytest.fixture
def repo_with_verbose_diff(tmp_path: Path) -> tuple[Path, str]:
    """Copy the verbose pair_01 fixture into a tmp repo and synthesize a
    minimal unified diff that mentions it (so ``_files_changed_for_secretscan``
    returns the path)."""
    src = tmp_path / "src"
    src.mkdir()
    shutil.copy(
        _FIXTURES_DIR / "pair_01_speculative_abstraction.py",
        src / "double.py",
    )
    diff = (
        "diff --git a/src/double.py b/src/double.py\n"
        "--- a/src/double.py\n"
        "+++ b/src/double.py\n"
        "@@ -0,0 +1 @@\n"
        "+# placeholder\n"
    )
    return tmp_path, diff


@pytest.mark.asyncio
async def test_code_size_gate_dispatched_when_enabled(
    repo_with_verbose_diff: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``cfg.qa_gates.code_size`` is True, the gate runs, surfaces a
    warn on the verbose fixture, and the dispatcher returns None (no halt)."""
    pytest.importorskip("radon")  # warn severity depends on radon loc metrics
    from orchestrator import execute_phase as ep

    cwd, diff = repo_with_verbose_diff

    captured: dict = {}

    async def spy_run_code_size(
        cwd_arg: Path,
        paths: list[Path] | None = None,
        edit_scope: list[str] | None = None,
        *,
        thresholds: object | None = None,
        baseline_enabled: bool = False,
        long_function_threshold: int | None = None,
    ) -> GateResult:
        captured["cwd"] = cwd_arg
        captured["paths"] = paths
        captured["thresholds"] = thresholds
        # Delegate to the real gate so we exercise the real code path.
        return await run_code_size(
            cwd_arg,
            paths=paths,
            edit_scope=edit_scope,
            thresholds=thresholds,
            baseline_enabled=baseline_enabled,
        )

    async def fake_pass(*_a: object, **_k: object) -> GateResult:
        return GateResult(passed=True, details="ok")

    monkeypatch.setattr(ep, "run_code_size", spy_run_code_size)
    monkeypatch.setattr(ep, "run_secretscan", lambda *a, **k: fake_pass())
    monkeypatch.setattr(ep, "run_syntax_check", lambda *a, **k: fake_pass())
    monkeypatch.setattr(ep, "run_lint", lambda *a, **k: fake_pass())
    monkeypatch.setattr(ep, "run_build_check", lambda *a, **k: fake_pass())
    monkeypatch.setattr(ep, "run_tests", lambda *a, **k: fake_pass())
    monkeypatch.setattr(ep, "run_hallucination_guard", lambda *a, **k: fake_pass())
    monkeypatch.setattr(ep, "detect_language", lambda *a, **k: "python")

    # Strict thresholds force a warn on verbose pair_01.
    class StrictThresholds:
        def model_dump(self) -> dict:
            return {
                "cyclomatic_max": 1,
                "loc_per_function": 100,
                "dead_symbols": 0,
                "commented_out_blocks": 0,
                "duplicate_clusters": 0,
            }

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
            build_check_timeout_s = 120.0  # WS2-11: build-gate timeout knob
            code_size = True
            code_size_baseline_enabled = False
            code_size_thresholds = StrictThresholds()

        hallucination_guard = False

    orch = type(
        "OrchStub",
        (),
        {"cfg": FakeCfg(), "cwd": cwd, "plugin_registry": None},
    )()

    class FakeTask:
        id = "1.1"
        metadata: dict = {}

    developer_result = AgentResult(
        text="ok",
        success=True,
        duration_s=0.1,
        diff=diff,
    )

    out = await ep._run_qa_gates(orch, FakeTask(), developer_result=developer_result)

    # Dispatcher returned None — no halt.
    assert out is None, f"expected no halt, got: {out!r}"
    # Gate was actually called with diff-scoped paths.
    assert captured.get("paths") == [Path("src/double.py")]
    assert captured.get("cwd") == cwd
    # Warning surfaced on the task metadata.
    warnings = FakeTask.metadata.get("qa_warnings", [])
    code_size_warns = [w for w in warnings if w["gate"] == "code_size"]
    assert len(code_size_warns) == 1
    assert code_size_warns[0]["severity"] == "warn"
    assert "metrics" in code_size_warns[0]
    assert "loc_executable" in code_size_warns[0]["metrics"]


@pytest.mark.asyncio
async def test_code_size_gate_skipped_when_disabled(
    repo_with_verbose_diff: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``cfg.qa_gates.code_size`` is False (default), the gate is NOT
    invoked and no warnings surface."""
    from orchestrator import execute_phase as ep

    cwd, diff = repo_with_verbose_diff

    spy_called = {"count": 0}

    async def spy_run_code_size(*a: object, **k: object) -> GateResult:
        spy_called["count"] += 1
        return GateResult(passed=True, severity="warn", details="should not see")

    async def fake_pass(*_a: object, **_k: object) -> GateResult:
        return GateResult(passed=True, details="ok")

    monkeypatch.setattr(ep, "run_code_size", spy_run_code_size)
    monkeypatch.setattr(ep, "run_secretscan", lambda *a, **k: fake_pass())
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
            lint_timeout_s = 120.0
            test_timeout_s = 600.0
            build_check_timeout_s = 120.0  # WS2-11: build-gate timeout knob
            code_size = False  # default
            code_size_baseline_enabled = False
            code_size_thresholds = None

        hallucination_guard = False

    orch = type(
        "OrchStub",
        (),
        {"cfg": FakeCfg(), "cwd": cwd, "plugin_registry": None},
    )()

    class FakeTask:
        id = "1.1"
        metadata: dict = {}

    developer_result = AgentResult(
        text="ok",
        success=True,
        duration_s=0.1,
        diff=diff,
    )

    out = await ep._run_qa_gates(orch, FakeTask(), developer_result=developer_result)
    assert out is None
    assert spy_called["count"] == 0
    assert FakeTask.metadata.get("qa_warnings", []) == []


@pytest.mark.asyncio
async def test_qagates_config_accepts_new_fields() -> None:
    """The new pydantic fields validate cleanly at the config layer."""
    from config.schema import CodeSizeThresholds, QAGatesConfig

    cfg = QAGatesConfig(
        code_size=True,
        code_size_baseline_enabled=False,
        code_size_thresholds=CodeSizeThresholds(loc_per_function=80),
    )
    assert cfg.code_size is True
    assert cfg.code_size_thresholds.loc_per_function == 80


@pytest.mark.asyncio
async def test_qagates_config_defaults_off() -> None:
    """code_size opt-in: defaults to False, threshold model is None."""
    from config.schema import QAGatesConfig

    cfg = QAGatesConfig()
    assert cfg.code_size is False
    assert cfg.code_size_baseline_enabled is False
    assert cfg.code_size_thresholds is None
