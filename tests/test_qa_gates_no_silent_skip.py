"""v0.27 (audit §6): diff-scoped QA gates must fail-closed on garbage diffs.

Three scenarios:

  1. ``extract_files_from_diff(strict=True)`` raises ``DiffParseError``
     when the diff body has content but no ``+++ b/`` headers.
  2. ``_files_changed_for_secretscan`` propagates the error.
  3. ``_run_qa_gates`` translates that into a fail-closed detail string
     for diff-producing tasks AND a clean skip for investigation tasks
     (``Task.produces_diff=False``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from adapters.git_utils import extract_files_from_diff
from adapters.types import AgentResult
from errors import DiffParseError
from fixtures.malformed_diffs import ALL_MALFORMED_DIFFS


# ---------------------------------------------------------------------------
# extract_files_from_diff: strict mode raises, lenient mode preserves
# v0.26.2 behavior (silent empty list).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,diff", ALL_MALFORMED_DIFFS)
def test_strict_mode_raises_for_malformed_diff(name: str, diff: str) -> None:
    """Strict mode raises ``DiffParseError`` for non-empty unparseable diffs."""
    with pytest.raises(DiffParseError) as exc_info:
        extract_files_from_diff(diff, strict=True)
    msg = str(exc_info.value)
    assert "no parseable" in msg
    # The error preview should include some characters from the diff.
    assert len(msg) > 0


@pytest.mark.parametrize("name,diff", ALL_MALFORMED_DIFFS)
def test_lenient_mode_returns_empty_for_malformed_diff(
    name: str, diff: str
) -> None:
    """Lenient (default) mode preserves v0.26.2 behaviour for legacy callers."""
    assert extract_files_from_diff(diff) == []
    # Explicit strict=False is the same.
    assert extract_files_from_diff(diff, strict=False) == []


def test_empty_diff_returns_empty_list_in_both_modes() -> None:
    """An empty / None diff is legitimately empty — never raises."""
    assert extract_files_from_diff("") == []
    assert extract_files_from_diff("", strict=True) == []


def test_valid_diff_returns_paths_in_both_modes() -> None:
    """A valid diff produces the same list in lenient + strict modes."""
    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    assert extract_files_from_diff(diff) == ["foo.py"]
    assert extract_files_from_diff(diff, strict=True) == ["foo.py"]


# ---------------------------------------------------------------------------
# _files_changed_for_secretscan: propagates DiffParseError to the gate
# site so it can decide based on Task.produces_diff.
# ---------------------------------------------------------------------------


def test_files_changed_for_secretscan_raises_on_garbage_diff() -> None:
    """Non-empty garbage diff → DiffParseError (was silently [] in v0.26.2)."""
    from orchestrator.execute_phase import _files_changed_for_secretscan

    result = AgentResult(
        text="ok",
        success=True,
        duration_s=0.1,
        diff="some garbage that has no diff headers at all",
    )
    with pytest.raises(DiffParseError):
        _files_changed_for_secretscan(result)


def test_files_changed_for_secretscan_returns_empty_for_legitimate_no_diff() -> None:
    """Legitimately absent diff (None / empty) → [], no exception."""
    from orchestrator.execute_phase import _files_changed_for_secretscan

    result_empty = AgentResult(
        text="ok", success=True, duration_s=0.1, diff=""
    )
    assert _files_changed_for_secretscan(result_empty) == []
    assert _files_changed_for_secretscan(None) == []


# ---------------------------------------------------------------------------
# _run_qa_gates: fail-closed for diff-producing tasks, skip for
# investigation tasks.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_qa_gates_fails_closed_on_garbage_diff_for_diff_task(
    tmp_path: Path,
) -> None:
    """v0.27 fail-closed semantics: a Task with ``produces_diff=True`` that
    ships a garbage diff body causes _run_qa_gates to return a failure
    detail string BEFORE any gate runs (no silent skip)."""
    from orchestrator import execute_phase as ep

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

    orch = type(
        "OrchStub",
        (),
        {"cfg": FakeCfg(), "cwd": tmp_path, "plugin_registry": None},
    )()

    class FakeTask:
        id = "1.1"
        produces_diff = True
        metadata: dict = {}

    developer_result = AgentResult(
        text="ok",
        success=True,
        duration_s=0.1,
        diff="garbage without any diff markers at all\n",
    )

    out = await ep._run_qa_gates(
        orch, FakeTask(), developer_result=developer_result
    )
    assert out is not None
    assert "unparseable" in out
    assert "secretscan" in out


@pytest.mark.asyncio
async def test_run_qa_gates_skips_diff_gate_for_investigation_task(
    tmp_path: Path,
) -> None:
    """When ``Task.produces_diff=False`` the garbage-diff fail-closed path
    is intentionally bypassed — investigation tasks legitimately have
    no diff to scan, so the gate skips with paths=[] instead of failing.
    """
    from orchestrator import execute_phase as ep
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

    import pytest as _pytest

    monkeypatch = _pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(ep, "run_secretscan", fake_run_secretscan)
        monkeypatch.setattr(
            ep, "run_syntax_check", lambda *a, **k: fake_pass()
        )
        monkeypatch.setattr(ep, "run_lint", lambda *a, **k: fake_pass())
        monkeypatch.setattr(
            ep, "run_build_check", lambda *a, **k: fake_pass()
        )
        monkeypatch.setattr(ep, "run_tests", lambda *a, **k: fake_pass())
        monkeypatch.setattr(
            ep, "detect_language", lambda *a, **k: "python"
        )

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

        orch = type(
            "OrchStub",
            (),
            {"cfg": FakeCfg(), "cwd": tmp_path, "plugin_registry": None},
        )()

        class FakeTask:
            id = "1.1"
            produces_diff = False
            metadata: dict = {}

        developer_result = AgentResult(
            text="ok",
            success=True,
            duration_s=0.1,
            diff="investigation report — no code changes here\n",
        )

        out = await ep._run_qa_gates(
            orch, FakeTask(), developer_result=developer_result
        )
        assert out is None  # all gates passed
        assert captured["paths"] == []  # diff-gate skipped via empty paths
    finally:
        monkeypatch.undo()


# ---------------------------------------------------------------------------
# secretscan empty-scope guard (audit §6.3): explicit info-severity skip
# when paths=[].
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_secretscan_returns_info_skip_for_empty_paths(
    tmp_path: Path,
) -> None:
    """``run_secretscan(paths=[])`` returns ``GateResult(severity="info",
    details="no files in diff scope")`` rather than the legacy
    "scanned nothing and passed" silent pass.
    """
    from qa.secretscan import run_secretscan

    result = await run_secretscan(tmp_path, paths=[])
    assert result.passed is True
    assert result.severity == "info"
    assert "no files in diff scope" in result.details
