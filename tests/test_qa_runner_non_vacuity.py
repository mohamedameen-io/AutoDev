"""Phase 1B / Group A — QA-runner non-vacuity + toolchain-absent degrade-loud.

Closes WS2-2 (vacuous 0-tests pass) and WS2-6 (toolchain-absent silent pass);
gates G1 and G2.

The defects being killed
------------------------
1. **Vacuous 0-tests pass (WS2-2).** ``test_runner`` returned ``passed=True``
   whenever ``rc == 0`` — but pytest exits ``0`` printing *"no tests ran"* when
   a non-empty scope collects zero tests (asyncio/conftest plugins suppress
   the exit-code-5 behaviour), and ``go test`` exits ``0`` printing
   *"[no test files]"* when source exists but no ``_test.go`` does. A gate that
   passes because it ran NOTHING is the bug.

2. **Toolchain-absent silent pass (WS2-6).** ``FileNotFoundError`` (the runner
   binary is not installed) was swallowed into ``passed=True`` in all three
   gates. A missing toolchain is *unknown*, not *clean*.

Engagement-first contract
-------------------------
The fix must DISTINGUISH three worlds and never silently green a found-nothing:

* (a) repo WITH code but ZERO tests  → ``passed=False``  (vacuous pass killed)
* (b) genuinely EMPTY repo / no source → ``passed=True`` (no_source signal)
* (c) intentional empty baseline      → ``passed=True`` (legitimate fast-exit)

RED-on-HEAD markers below tag the assertions that VACUOUSLY PASS today and must
flip to fail-loud after the fix. The broken-control tests prove the fix is the
thing carrying the weight (revert the parse / the FileNotFoundError branch →
red).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qa.build_check import run_build_check
from qa.test_runner import run_tests


def _make_proc(returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


# ---------------------------------------------------------------------------
# G1(a) — repo with CODE but ZERO tests → passed=False (vacuous pass killed).
#
# RED-on-HEAD: today the runner returns passed=True because rc==0 is the only
# check; pytest printed "no tests ran" but rc was 0. This assertion FAILS today.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_python_code_present_zero_tests_fails_loud(tmp_path: Path) -> None:
    """pytest scope is non-empty but collects 0 tests (rc==0, 'no tests ran')."""
    # Source exists (a non-empty scope), but pytest "ran nothing".
    (tmp_path / "app.py").write_text("def add(a, b):\n    return a + b\n")
    proc = _make_proc(0, stdout=b"no tests ran in 0.01s\n")
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await run_tests(tmp_path, language="python")
    # RED-on-HEAD: vacuously True today.
    assert result.passed is False, (
        "0 tests collected in a non-empty scope must NOT be a clean pass "
        f"(got passed={result.passed!r}, details={result.details!r})"
    )
    blob = (result.details + repr(getattr(result, "metrics", {}))).lower()
    assert "no_test_coverage" in blob or "no tests" in blob or "scope" in blob, (
        f"loud no-coverage signal missing: details={result.details!r} "
        f"metrics={getattr(result, 'metrics', {})!r}"
    )


@pytest.mark.asyncio
async def test_python_zero_passed_token_fails_loud(tmp_path: Path) -> None:
    """A '0 passed' summary with rc==0 in a non-empty scope also fails loud."""
    (tmp_path / "app.py").write_text("x = 1\n")
    proc = _make_proc(0, stdout=b"0 passed in 0.02s\n")
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await run_tests(tmp_path, language="python")
    # RED-on-HEAD: vacuously True today.
    assert result.passed is False


@pytest.mark.asyncio
async def test_go_code_present_no_test_files_fails_loud(tmp_path: Path) -> None:
    """go test prints '[no test files]' with rc==0 when code has no _test.go."""
    (tmp_path / "go.mod").write_text("module example.com/m\ngo 1.21\n")
    (tmp_path / "main.go").write_text("package main\nfunc main() {}\n")
    proc = _make_proc(0, stdout=b"?   \texample.com/m\t[no test files]\n")
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await run_tests(tmp_path, language="go")
    # RED-on-HEAD: vacuously True today.
    assert result.passed is False, (
        "go: code present but '[no test files]' must not be a clean pass "
        f"(got passed={result.passed!r}, details={result.details!r})"
    )


# ---------------------------------------------------------------------------
# G1(b) — genuinely EMPTY repo (no source, no manifest) → passed=True with a
# no_source / language_not_detected signal. (Already correct via detect_language
# returning None — guards against a regression that over-fails empty repos.)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_repo_passes_with_no_source_signal(tmp_path: Path) -> None:
    """No manifest at all → language not detected → clean pass, NOT a fail."""
    result = await run_tests(tmp_path)  # auto-detect → None
    assert result.passed is True
    assert "not detected" in result.details.lower()


@pytest.mark.asyncio
async def test_python_zero_tests_but_empty_scope_passes(tmp_path: Path) -> None:
    """Python detected but the scope holds NO source files (only the manifest):
    'no tests ran' is legitimate here — there is nothing to cover yet."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\n")
    # No *.py source anywhere → an empty source scope.
    proc = _make_proc(0, stdout=b"no tests ran in 0.01s\n")
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await run_tests(tmp_path, language="python")
    assert result.passed is True, (
        "empty source scope (manifest only, zero .py) must keep the legitimate "
        f"fast-exit pass (got passed={result.passed!r}, details={result.details!r})"
    )


# ---------------------------------------------------------------------------
# G1(c) — intentional empty baseline (paths-scoped, no python changes) →
# passed=True. The legacy fast-exit must survive.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_baseline_no_python_changes_passes(tmp_path: Path) -> None:
    """paths=[non-python] → 'no python changes' fast-exit stays a clean pass."""
    with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
        result = await run_tests(
            tmp_path, language="python", paths=[Path("docs/readme.md")]
        )
    assert result.passed is True
    assert "no python changes" in result.details
    mock_exec.assert_not_called()


# ---------------------------------------------------------------------------
# G1 — three-way distinction sanity: a REAL passing run (1 passed) stays green.
# Proves the fix does not over-fail legitimate non-empty runs.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_python_real_pass_stays_green(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("x = 1\n")
    proc = _make_proc(0, stdout=b"5 passed in 0.10s\n")
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await run_tests(tmp_path, language="python")
    assert result.passed is True


@pytest.mark.asyncio
async def test_go_real_pass_stays_green(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/m\ngo 1.21\n")
    (tmp_path / "main.go").write_text("package main\nfunc main() {}\n")
    proc = _make_proc(0, stdout=b"ok  \texample.com/m\t0.5s\n")
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await run_tests(tmp_path, language="go")
    assert result.passed is True


# ---------------------------------------------------------------------------
# G2 — toolchain-absent degrade-LOUD (WS2-6).
#
# RED-on-HEAD: FileNotFoundError currently → passed=True ("not found, skipping").
# After the fix → passed=False with a 'skipped_toolchain_missing' /
# 'toolchain not installed' signal.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_check_toolchain_missing_fails_loud(tmp_path: Path) -> None:
    """run_build_check(go) with the toolchain absent → NOT a clean pass."""
    (tmp_path / "go.mod").write_text("module example.com/m\ngo 1.21\n")
    with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
        result = await run_build_check(tmp_path, language="go")
    # RED-on-HEAD: vacuously True today.
    assert result.passed is False, (
        "missing build toolchain must degrade loud, not silently pass "
        f"(got passed={result.passed!r}, details={result.details!r})"
    )
    blob = (result.details + repr(getattr(result, "metrics", {}))).lower()
    assert "toolchain" in blob or "not installed" in blob or "skipped_toolchain" in blob, (
        f"loud toolchain-missing signal absent: details={result.details!r} "
        f"metrics={getattr(result, 'metrics', {})!r}"
    )


@pytest.mark.asyncio
async def test_test_runner_toolchain_missing_fails_loud(tmp_path: Path) -> None:
    """run_tests with the runner binary absent → degrade loud, not pass."""
    with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
        result = await run_tests(tmp_path, language="python")
    # RED-on-HEAD: vacuously True today.
    assert result.passed is False
    blob = (result.details + repr(getattr(result, "metrics", {}))).lower()
    assert "toolchain" in blob or "not installed" in blob or "skipped_toolchain" in blob


@pytest.mark.asyncio
async def test_build_check_python_compiler_missing_fails_loud(tmp_path: Path) -> None:
    """The python-build inner FileNotFoundError branch must also degrade loud."""
    (tmp_path / "app.py").write_text("x = 1\n")
    # First create_subprocess_exec call (py_compile) raises FileNotFoundError.
    with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
        result = await run_build_check(tmp_path, language="python")
    # RED-on-HEAD: vacuously True today.
    assert result.passed is False


@pytest.mark.asyncio
async def test_g2_real_path_with_real_go_absent(tmp_path: Path) -> None:
    """G2 with a real (un-mocked) absent binary: invoke a non-existent runner.

    Skipped only if 'go' actually IS on PATH and we cannot easily hide it; the
    mocked variant above is the primary guarantee, this is the belt-and-braces
    real-subprocess proof that FileNotFoundError really fires the loud branch.
    """
    (tmp_path / "go.mod").write_text("module example.com/m\ngo 1.21\n")
    # Patch the go runner's argv to a guaranteed-absent binary so the real
    # asyncio subprocess machinery raises FileNotFoundError for real.
    from qa import build_check as bc

    async def _fake_go(cwd: Path, *, timeout_s: float):  # type: ignore[no-untyped-def]
        return await bc._run_subprocess(
            ["autodev-nonexistent-go-binary-xyz", "build", "./..."],
            cwd,
            timeout_s=timeout_s,
            tool_name="go build",
        )

    with patch.object(bc, "_run_go_build", _fake_go):
        result = await run_build_check(tmp_path, language="go")
    assert result.passed is False, (
        f"real absent binary must degrade loud (details={result.details!r})"
    )


# ---------------------------------------------------------------------------
# BROKEN-CONTROL — reverting the fix re-introduces the vacuous pass.
#
# These monkeypatch the *fixed* internals back to their old behaviour and prove
# the test then goes red (i.e. the assertions above are load-bearing on the new
# code, not on incidental output strings).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_broken_control_vacuous_pass_returns_if_count_check_removed(
    tmp_path: Path,
) -> None:
    """If we simulate the OLD rc==0-only logic, the loud assertion would fail.

    We assert the *negation*: under a stub that ignores the count, the result is
    a (wrong) clean pass — documenting exactly what the fix prevents. If a
    future refactor makes ``run_tests`` ignore the count again, the primary
    G1(a) test reverts to RED; this control just pins the mechanism.
    """
    (tmp_path / "app.py").write_text("x = 1\n")
    proc = _make_proc(0, stdout=b"no tests ran in 0.01s\n")

    # Stub the count parser to always report "tests ran" (the broken state).
    import qa.test_runner as tr

    if not hasattr(tr, "_classify_run_count"):
        pytest.skip("fix not present yet (RED phase): nothing to revert")
    with patch.object(tr, "_classify_run_count", return_value=("ran", 1)):
        with patch(
            "asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
        ):
            result = await run_tests(tmp_path, language="python")
    # With the parser neutered, the vacuous pass returns — proving the parser
    # is what carries the loud-fail.
    assert result.passed is True


@pytest.mark.asyncio
async def test_broken_control_toolchain_missing_passes_if_branch_reverted(
    tmp_path: Path,
) -> None:
    """Pin the FileNotFoundError loud-fail to a single helper: if a helper that
    re-raises into a clean pass is reinstated, G2 reverts to RED. Here we prove
    the loud branch is reached by checking it is NOT a pass under the fix."""
    (tmp_path / "go.mod").write_text("module example.com/m\ngo 1.21\n")
    with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
        result = await run_build_check(tmp_path, language="go")
    # Under the FIX this is False. (The broken control is the inverse encoded in
    # the primary G2 test — reverting build_check's FileNotFoundError branch to
    # passed=True flips test_build_check_toolchain_missing_fails_loud to RED.)
    assert result.passed is False
    # Belt-and-braces: real absent binary on the un-mocked path.
    assert shutil.which("autodev-nonexistent-go-binary-xyz") is None
