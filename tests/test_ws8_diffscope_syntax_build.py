"""WS8: diff-scope the ``syntax_check`` / ``build_check`` QA gates.

Root cause (confirmed): ``syntax_check`` and ``build_check`` were the only
two gates in the QA dispatch list (``orchestrator.execute_phase._run_qa_gates``,
the ``gates: list[...]`` triples) invoked WITHOUT ``paths=secretscan_paths`` --
every sibling gate (``lint``, ``test_runner``, ``secretscan``,
``hallucination_guard``, ``mutation_test``, ``code_size``, ``reproduce_gate``,
``debug_tag``) is diff-scoped. The omission made these two gates silently scan
the ENTIRE repo tree, including pre-existing, untouched files -- exactly how a
linter's own intentionally-malformed test fixtures get collected and treated
as a genuine regression the current task introduced.

Fix: forward ``paths=secretscan_paths`` into both gate calls, mirroring every
sibling gate. ``qa/syntax_check.py`` / ``qa/build_check.py`` already fully
implement ``paths=`` scoping (S2 / WS2-16) -- nothing there changes.

These tests drive the REAL subprocess-backed gates (no mocking of the
py_compile step) against real files on disk, so the "ignores untouched files"
tests are RED under the pre-fix wiring (whole-tree scan trips over the
untouched file's pre-existing ``SyntaxError``) and GREEN once ``paths=`` is
forwarded -- proving the scoping is load-bearing, not vacuous. The
"still blocks on a touched file" tests are controls: they must stay GREEN in
both states, proving the fix loses zero genuine bug-catching power.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from adapters.types import AgentResult


# A syntactically invalid Python file: an unclosed parameter list. Real
# py_compile raises a genuine SyntaxError on this -- no mocking involved.
_BROKEN_PY = "def broken(:\n    pass\n"


def _make_orch_stub(
    tmp_path: Path, *, syntax_check: bool = True, build_check: bool = True
) -> object:
    """A minimal ``Orchestrator`` stand-in with every OTHER gate disabled so
    only ``syntax_check`` / ``build_check`` are exercised for real."""

    class _QAGates:
        pass

    _QAGates.syntax_check = syntax_check
    _QAGates.lint = False
    _QAGates.build_check = build_check
    _QAGates.test_runner = False
    _QAGates.secretscan = False
    _QAGates.secretscan_baseline_enabled = False
    _QAGates.secretscan_per_extension_thresholds = None
    _QAGates.mutation_test_enabled = False
    _QAGates.mutation_test_threshold = 0.7
    _QAGates.lint_timeout_s = 120.0
    _QAGates.test_timeout_s = 600.0
    _QAGates.build_check_timeout_s = 120.0

    class FakeCfg:
        hallucination_guard = False  # top-level attr; disables that gate too

        qa_gates = _QAGates()

    return type(
        "OrchStub",
        (),
        {
            "cfg": FakeCfg(),
            "cwd": tmp_path,
            "plugin_registry": None,
        },
    )()


class _FakeTask:
    id = "1.1"


def _diff_touching(*filenames: str) -> str:
    """A minimal parseable diff body (``+++ b/<name>`` headers only), matching
    the format already exercised by
    ``tests/test_orchestrator_execute_phase_secretscan.py``."""
    return "".join(f"+++ b/{name}\n" for name in filenames)


# ---------------------------------------------------------------------------
# ENGAGEMENT: a pre-existing SyntaxError in a file NOT in the diff must not
# block the gate -- diff-scoping means only the executor's just-touched files
# are compiled this turn.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_syntax_check_ignores_untouched_files_pre_existing_syntax_error(
    tmp_path: Path,
) -> None:
    (tmp_path / "untouched_broken.py").write_text(_BROKEN_PY, encoding="utf-8")
    (tmp_path / "touched.py").write_text("x = 1\n", encoding="utf-8")

    from orchestrator import execute_phase as ep

    # Isolate syntax_check: build_check OFF so only the syntax gate is exercised
    # (mirrors the syntax_check=False in the build_check counterpart below).
    orch = _make_orch_stub(tmp_path, build_check=False)
    developer_result = AgentResult(
        text="ok", success=True, duration_s=0.1, diff=_diff_touching("touched.py")
    )

    out = await ep._run_qa_gates(orch, _FakeTask(), developer_result=developer_result)

    assert out is None, (
        "syntax_check must PASS when only a pre-existing, untouched file has a "
        f"SyntaxError; the gate must be diff-scoped. Got failure: {out!r}"
    )


@pytest.mark.asyncio
async def test_build_check_ignores_untouched_files_pre_existing_syntax_error(
    tmp_path: Path,
) -> None:
    (tmp_path / "untouched_broken.py").write_text(_BROKEN_PY, encoding="utf-8")
    (tmp_path / "touched.py").write_text("x = 1\n", encoding="utf-8")

    from orchestrator import execute_phase as ep

    orch = _make_orch_stub(tmp_path, syntax_check=False, build_check=True)
    developer_result = AgentResult(
        text="ok", success=True, duration_s=0.1, diff=_diff_touching("touched.py")
    )

    out = await ep._run_qa_gates(orch, _FakeTask(), developer_result=developer_result)

    assert out is None, (
        "build_check must PASS when only a pre-existing, untouched file has a "
        f"SyntaxError; the gate must be diff-scoped. Got failure: {out!r}"
    )


# ---------------------------------------------------------------------------
# CONTROL: a SyntaxError in a file that IS in the diff must still block the
# gate -- scoping must not blind the gate to genuine regressions. These stay
# green both before and after the fix.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_syntax_check_still_blocks_on_touched_file_syntax_error(
    tmp_path: Path,
) -> None:
    (tmp_path / "touched_broken.py").write_text(_BROKEN_PY, encoding="utf-8")

    from orchestrator import execute_phase as ep

    orch = _make_orch_stub(tmp_path)
    developer_result = AgentResult(
        text="ok",
        success=True,
        duration_s=0.1,
        diff=_diff_touching("touched_broken.py"),
    )

    out = await ep._run_qa_gates(orch, _FakeTask(), developer_result=developer_result)

    assert out is not None, "syntax_check must still BLOCK a genuine syntax error in a touched file"
    assert "touched_broken.py" in out


@pytest.mark.asyncio
async def test_build_check_still_blocks_on_touched_file_syntax_error(
    tmp_path: Path,
) -> None:
    (tmp_path / "touched_broken.py").write_text(_BROKEN_PY, encoding="utf-8")

    from orchestrator import execute_phase as ep

    orch = _make_orch_stub(tmp_path, syntax_check=False, build_check=True)
    developer_result = AgentResult(
        text="ok",
        success=True,
        duration_s=0.1,
        diff=_diff_touching("touched_broken.py"),
    )

    out = await ep._run_qa_gates(orch, _FakeTask(), developer_result=developer_result)

    assert out is not None, "build_check must still BLOCK a genuine syntax error in a touched file"
    assert "touched_broken.py" in out


# ---------------------------------------------------------------------------
# BROKEN-CONTROL: forcing the legacy (pre-fix) no-``paths`` call reproduces the
# whole-tree false-positive -- proving the "ignores untouched files" contract
# above is load-bearing, not vacuous (mirrors the broken-control convention in
# tests/test_should_ws2_build.py).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_broken_control_syntax_check_whole_tree_scan_blocks_on_untouched_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BROKEN-CONTROL: a gate call site that drops ``paths=`` (today's
    pre-fix ``syntax_check`` wiring) makes the untouched file's pre-existing
    ``SyntaxError`` block the gate again -- exactly the false-positive the fix
    removes."""
    (tmp_path / "untouched_broken.py").write_text(_BROKEN_PY, encoding="utf-8")
    (tmp_path / "touched.py").write_text("x = 1\n", encoding="utf-8")

    from orchestrator import execute_phase as ep
    from qa.syntax_check import run_syntax_check as real_run_syntax_check

    async def legacy_whole_tree_syntax_check(cwd: Path, language: str | None = None, **_ignored: object):
        # Simulates the PRE-FIX call site: ``paths=`` is never forwarded, so
        # this always degrades to the whole-tree walk regardless of what the
        # caller passes.
        return await real_run_syntax_check(cwd, language)

    monkeypatch.setattr(ep, "run_syntax_check", legacy_whole_tree_syntax_check)

    orch = _make_orch_stub(tmp_path)
    developer_result = AgentResult(
        text="ok", success=True, duration_s=0.1, diff=_diff_touching("touched.py")
    )

    out = await ep._run_qa_gates(orch, _FakeTask(), developer_result=developer_result)

    assert out is not None, (
        "broken control: dropping paths= must reproduce the whole-tree "
        "false-positive (untouched_broken.py should block the gate)"
    )
    assert "untouched_broken.py" in out


@pytest.mark.asyncio
async def test_broken_control_build_check_whole_tree_scan_blocks_on_untouched_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BROKEN-CONTROL: same as above, for the ``build_check`` call site."""
    (tmp_path / "untouched_broken.py").write_text(_BROKEN_PY, encoding="utf-8")
    (tmp_path / "touched.py").write_text("x = 1\n", encoding="utf-8")

    from orchestrator import execute_phase as ep
    from qa.build_check import run_build_check as real_run_build_check

    async def legacy_whole_tree_build_check(
        cwd: Path, language: str | None = None, *, timeout_s: float = 120.0, **_ignored: object
    ):
        # Simulates the PRE-FIX call site: ``paths=`` is never forwarded.
        return await real_run_build_check(cwd, language, timeout_s=timeout_s)

    monkeypatch.setattr(ep, "run_build_check", legacy_whole_tree_build_check)

    orch = _make_orch_stub(tmp_path, syntax_check=False, build_check=True)
    developer_result = AgentResult(
        text="ok", success=True, duration_s=0.1, diff=_diff_touching("touched.py")
    )

    out = await ep._run_qa_gates(orch, _FakeTask(), developer_result=developer_result)

    assert out is not None, (
        "broken control: dropping paths= must reproduce the whole-tree "
        "false-positive (untouched_broken.py should block the gate)"
    )
    assert "untouched_broken.py" in out
