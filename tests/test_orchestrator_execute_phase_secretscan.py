"""Tests for v0.13.0 diff-scope wiring at the secretscan gate site.

The orchestrator's ``_run_qa_gates`` invokes ``run_secretscan`` with the
developer's diff paths so the gate scans only the files the executor
just modified — pre-existing repo state is no longer the executor's
concern.

Also covers the small helper ``_files_changed_for_secretscan`` that
extracts repo-relative paths from a developer ``AgentResult``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from adapters.types import AgentResult


# ---------------------------------------------------------------------------
# _files_changed_for_secretscan: extracts repo-relative paths from
# the developer's AgentResult. Returns None when no diff is available.
# ---------------------------------------------------------------------------


def test_files_changed_for_secretscan_extracts_from_diff() -> None:
    from orchestrator.execute_phase import _files_changed_for_secretscan

    result = AgentResult(
        text="ok",
        success=True,
        duration_s=0.1,
        diff=(
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1 +1 @@\n"
            "-x = 1\n"
            "+x = 2\n"
            "diff --git a/bar.py b/bar.py\n"
            "+++ b/bar.py\n"
        ),
    )
    paths = _files_changed_for_secretscan(result)
    assert paths == [Path("foo.py"), Path("bar.py")]


def test_files_changed_for_secretscan_returns_empty_list_when_no_diff() -> None:
    """No diff → empty list (v0.26.1 patch C: callers scan nothing).

    Previously the function returned ``None`` to signal "fall back to a
    legacy full-walk". On huge vendored trees the full-walk was a
    footgun (the 2026-05-11 Unity / SDL2 incident). The new contract:
    "no diff → scan no files".
    """
    from orchestrator.execute_phase import _files_changed_for_secretscan

    result = AgentResult(
        text="ok",
        success=True,
        duration_s=0.1,
        diff=None,
    )
    assert _files_changed_for_secretscan(result) == []


def test_files_changed_for_secretscan_returns_empty_list_when_developer_result_none() -> None:
    """v0.26.1 patch C: ``None`` developer_result also yields an empty list."""
    from orchestrator.execute_phase import _files_changed_for_secretscan

    assert _files_changed_for_secretscan(None) == []


def test_files_changed_for_secretscan_returns_empty_list_when_diff_empty() -> None:
    """An empty-string diff yields an empty list (caller may skip the gate)."""
    from orchestrator.execute_phase import _files_changed_for_secretscan

    result = AgentResult(
        text="ok",
        success=True,
        duration_s=0.1,
        diff="",
    )
    assert _files_changed_for_secretscan(result) == []


def test_files_changed_for_secretscan_returns_empty_when_no_paths_in_diff() -> None:
    """A non-empty diff with no ``+++ b/`` headers (e.g. error output) → []."""
    from orchestrator.execute_phase import _files_changed_for_secretscan

    result = AgentResult(
        text="ok",
        success=True,
        duration_s=0.1,
        diff="some non-diff text",
    )
    assert _files_changed_for_secretscan(result) == []


# ---------------------------------------------------------------------------
# _run_qa_gates: when a developer_result is supplied with a diff, the
# secretscan gate is invoked with the extracted paths list.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_secretscan_invoked_with_developer_diff_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate site passes ``paths=[diff paths]`` into run_secretscan when
    a developer result is available."""
    from plugins.registry import GateResult

    captured: dict = {}

    async def fake_run_secretscan(
        cwd: Path,
        paths: list[Path] | None = None,
        edit_scope: list[str] | None = None,
        per_extension_thresholds: dict[str, float] | None = None,
        baseline_enabled: bool = False,
    ) -> GateResult:
        captured["cwd"] = cwd
        captured["paths"] = paths
        return GateResult(passed=True, details="ok")

    # Stub other gates to no-ops so secretscan is the only one we observe.
    async def fake_pass(*_a: object, **_k: object) -> GateResult:
        return GateResult(passed=True, details="ok")

    from orchestrator import execute_phase as ep

    monkeypatch.setattr(ep, "run_secretscan", fake_run_secretscan)
    monkeypatch.setattr(ep, "run_syntax_check", lambda *a, **k: fake_pass())
    monkeypatch.setattr(ep, "run_lint", lambda *a, **k: fake_pass())
    monkeypatch.setattr(ep, "run_build_check", lambda *a, **k: fake_pass())
    monkeypatch.setattr(ep, "run_tests", lambda *a, **k: fake_pass())
    monkeypatch.setattr(ep, "detect_language", lambda *a, **k: "python")

    # Minimal orch stub.
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

    orch = type(
        "OrchStub",
        (),
        {
            "cfg": FakeCfg(),
            "cwd": tmp_path,
            "plugin_registry": None,
        },
    )()

    class FakeTask:
        id = "1.1"

    developer_result = AgentResult(
        text="ok",
        success=True,
        duration_s=0.1,
        diff="+++ b/file_a.py\n+++ b/file_b.py\n",
    )

    out = await ep._run_qa_gates(orch, FakeTask(), developer_result=developer_result)
    assert out is None  # gates passed
    assert captured["cwd"] == tmp_path
    assert captured["paths"] == [Path("file_a.py"), Path("file_b.py")]


@pytest.mark.asyncio
async def test_secretscan_invoked_with_empty_paths_when_no_developer_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v0.26.1 patch C: when developer_result is None, secretscan receives
    ``paths=[]`` ("scan nothing") instead of ``None`` (legacy full walk)."""
    from plugins.registry import GateResult

    captured: dict = {}

    async def fake_run_secretscan(
        cwd: Path,
        paths: list[Path] | None = None,
        edit_scope: list[str] | None = None,
        per_extension_thresholds: dict[str, float] | None = None,
        baseline_enabled: bool = False,
    ) -> GateResult:
        captured["paths"] = paths
        return GateResult(passed=True, details="ok")

    async def fake_pass(*_a: object, **_k: object) -> GateResult:
        return GateResult(passed=True, details="ok")

    from orchestrator import execute_phase as ep

    monkeypatch.setattr(ep, "run_secretscan", fake_run_secretscan)
    monkeypatch.setattr(ep, "run_syntax_check", lambda *a, **k: fake_pass())
    monkeypatch.setattr(ep, "run_lint", lambda *a, **k: fake_pass())
    monkeypatch.setattr(ep, "run_build_check", lambda *a, **k: fake_pass())
    monkeypatch.setattr(ep, "run_tests", lambda *a, **k: fake_pass())
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

    orch = type(
        "OrchStub",
        (),
        {
            "cfg": FakeCfg(),
            "cwd": tmp_path,
            "plugin_registry": None,
        },
    )()

    class FakeTask:
        id = "1.1"

    out = await ep._run_qa_gates(orch, FakeTask(), developer_result=None)
    assert out is None
    assert captured["paths"] == []


@pytest.mark.asyncio
async def test_hallucination_guard_passes_empty_paths_when_no_developer_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v0.26.1 patch C: hallucination_guard receives ``paths=[]`` when there
    is no developer diff. Previously it walked the full tree (the v0.13.0
    legacy fallback) which on huge vendored trees both wasted budget and
    surfaced encoding crashes.
    """
    from plugins.registry import GateResult

    captured: dict = {}

    async def fake_hallucination_guard(
        cwd: Path,
        paths: list[Path] | None = None,
        **_k: object,
    ) -> GateResult:
        captured["paths"] = paths
        return GateResult(passed=True, details="ok")

    async def fake_pass(*_a: object, **_k: object) -> GateResult:
        return GateResult(passed=True, details="ok")

    from orchestrator import execute_phase as ep

    monkeypatch.setattr(ep, "run_hallucination_guard", fake_hallucination_guard)
    monkeypatch.setattr(ep, "run_syntax_check", lambda *a, **k: fake_pass())
    monkeypatch.setattr(ep, "run_lint", lambda *a, **k: fake_pass())
    monkeypatch.setattr(ep, "run_build_check", lambda *a, **k: fake_pass())
    monkeypatch.setattr(ep, "run_tests", lambda *a, **k: fake_pass())
    monkeypatch.setattr(ep, "run_secretscan", lambda *a, **k: fake_pass())
    monkeypatch.setattr(ep, "detect_language", lambda *a, **k: "python")

    class FakeCfg:
        hallucination_guard = True

        class qa_gates:
            syntax_check = True
            lint = True
            build_check = True
            test_runner = True
            secretscan = False  # disable so we observe hallucination_guard
            secretscan_baseline_enabled = False
            secretscan_per_extension_thresholds = None
            mutation_test_enabled = False
            mutation_test_threshold = 0.7

    orch = type(
        "OrchStub",
        (),
        {
            "cfg": FakeCfg(),
            "cwd": tmp_path,
            "plugin_registry": None,
        },
    )()

    class FakeTask:
        id = "1.1"

    out = await ep._run_qa_gates(orch, FakeTask(), developer_result=None)
    assert out is None
    assert captured["paths"] == []
