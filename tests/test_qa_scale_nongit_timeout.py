"""Phase 3 / qa-scale gate tests: non-git full-suite fallback + build_check timeout knob.

DEFECT 1 (WS2-14)
-----------------
For a NON-git in-place repo the changed-file diff is git-only → an EMPTY diff →
``run_tests(paths=[])``. Today ``paths=[]`` maps onto the *clean git repo, no
changes* branch and returns a vacuous ``passed=True`` "no python changes" pass —
the test gate ran NOTHING. A non-git repo has *no git signal at all*, which is
NOT the same as "a git repo with no changes". When there is no git signal we must
FALL BACK to the full suite rather than vacuously passing.

* A NON-git repo with source+tests → the gate runs the FULL suite.
* A git repo (``.git`` present) with no changes → still the legit empty
  behavior (no subprocess, "no python changes" pass).

DEFECT 2 (WS2-11)
-----------------
``build_check`` had a hardcoded 60s timeout with no config knob, so cold
cargo/Go builds time out. ``cfg.build_check_timeout_s`` (default >= 120) must
exist and be honored by ``run_build_check`` (the timeout passed to the runner
reflects the cfg).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config.schema import QAGatesConfig
from qa.build_check import run_build_check
from qa.test_runner import run_tests


def _make_proc(returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


# --------------------------------------------------------------------------- #
# DEFECT 1 (WS2-14): non-git → full-suite fallback (not a vacuous scoped pass) #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_nongit_empty_paths_runs_full_suite(tmp_path: Path) -> None:
    """NON-git repo + source/tests + empty diff (paths=[]) → run the FULL suite.

    RED-on-HEAD: today ``paths=[]`` returns a vacuous "no python changes" pass
    with NO subprocess spawned. The non-git case has no git signal, so it must
    fall back to the bare full suite (``pytest -q``).
    """
    # Non-git repo: NO .git directory. Has source + a test.
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("def test_x():\n    assert True\n")

    proc = _make_proc(0, stdout=b"1 passed")
    with patch(
        "asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
    ) as mock_exec:
        result = await run_tests(tmp_path, language="python", paths=[])

    # Full suite must have actually been invoked (NOT a vacuous scoped pass).
    mock_exec.assert_called()
    args = list(mock_exec.call_args.args)
    assert args[0] == "pytest"
    # Bare full suite: no scoped target, just ``-q``.
    assert args == ["pytest", "-q"]
    assert result.passed
    assert "no python changes" not in result.details


@pytest.mark.asyncio
async def test_git_repo_empty_paths_keeps_legit_empty_behavior(tmp_path: Path) -> None:
    """GIT repo (``.git`` present) + empty diff (paths=[]) → legit empty pass.

    A clean git repo with no changes legitimately scopes to nothing: keep the
    "no python changes" fast-exit pass with NO subprocess spawned. This is the
    control that keeps the non-git fallback from over-firing on real git repos.
    """
    (tmp_path / ".git").mkdir()  # git signal present
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n")

    with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
        result = await run_tests(tmp_path, language="python", paths=[])

    mock_exec.assert_not_called()
    assert result.passed
    assert "no python changes" in result.details


# --------------------------------------------------------------------------- #
# DEFECT 2 (WS2-11): build_check_timeout_s config knob, honored by build_check #
# --------------------------------------------------------------------------- #


def test_build_check_timeout_cfg_default_at_least_120() -> None:
    """``cfg.build_check_timeout_s`` exists with a default >= 120s.

    RED-on-HEAD: the field does not exist (AttributeError) and ``extra='forbid'``
    rejects it as a kwarg.
    """
    cfg = QAGatesConfig()
    assert hasattr(cfg, "build_check_timeout_s")
    assert cfg.build_check_timeout_s >= 120
    # Operator-overridable.
    cfg2 = QAGatesConfig(build_check_timeout_s=300)
    assert cfg2.build_check_timeout_s == 300


@pytest.mark.asyncio
async def test_build_check_honors_cfg_timeout(tmp_path: Path) -> None:
    """The timeout passed to the runner reflects ``cfg.build_check_timeout_s``.

    Capture the ``timeout=`` kwarg ``run_build_check`` hands to ``asyncio.wait_for``
    and assert it equals the configured value (not the legacy hardcoded 60s).
    """
    cfg = QAGatesConfig()
    cfg_timeout = cfg.build_check_timeout_s

    captured: list[float] = []

    real_wait_for = __import__("asyncio").wait_for

    async def _spy_wait_for(awaitable, timeout=None):  # type: ignore[no-untyped-def]
        captured.append(timeout)
        return await real_wait_for(awaitable, timeout=timeout)

    proc = _make_proc(0)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        with patch("asyncio.wait_for", new=_spy_wait_for):
            result = await run_build_check(
                tmp_path, language="rust", timeout_s=cfg_timeout
            )

    assert result.passed
    # Every wait_for in the build subprocess path used the cfg timeout.
    assert captured, "asyncio.wait_for was never called"
    assert all(t == cfg_timeout for t in captured)
    assert cfg_timeout >= 120
