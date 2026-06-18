"""Gate tests for WS2-8 (pure-TS syntax check) and WS2-10 (cargo clippy --workspace).

WS2-8 — Pure-TS repo syntax check
---------------------------------
Before the fix, ``_nodejs_syntax_check`` globbed ONLY ``*.js``. A pure-TypeScript
repo (a ``package.json`` plus ``.ts`` files and no ``.js``) therefore produced a
*vacuous* "no .js files found" pass — a real ``.ts`` syntax error sailed through
the gate undetected. ``node --check`` cannot be used directly on TS (it rejects
valid type annotations), so the fix runs a TS-aware compiler (``tsc --noEmit``)
over the ``.ts``/``.tsx`` files and degrades LOUD when no TS toolchain is
resolvable, instead of vacuous-passing.

WS2-10 — cargo clippy --workspace
---------------------------------
``cargo clippy`` without ``--workspace`` only lints the current package, missing
sibling crates in a Cargo workspace. The fix appends ``--workspace``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qa.lint import run_lint
from qa.syntax_check import run_syntax_check


def _make_proc(returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


# ---------------------------------------------------------------------------
# WS2-8: pure-TS repo with a syntax error must be CAUGHT, not vacuous-pass.
# ---------------------------------------------------------------------------


def _make_pure_ts_repo(tmp_path: Path) -> None:
    """A repo detected as nodejs (package.json) holding ONLY .ts files."""
    (tmp_path / "package.json").write_text('{"name": "pure-ts"}\n')
    # A genuine TS syntax error (unclosed paren). tsc would flag it.
    (tmp_path / "broken.ts").write_text("function broken( {\n")


@pytest.mark.asyncio
async def test_pure_ts_syntax_error_is_caught(tmp_path: Path) -> None:
    """RED-on-HEAD: today a pure-TS repo returns 'no .js files found' (vacuous pass).

    After the fix the gate invokes a TS compiler; a non-zero return means the
    syntax error is reported (passed=False), and the message must NOT be the
    vacuous 'no .js files' pass.
    """
    _make_pure_ts_repo(tmp_path)
    # tsc resolvable and reporting a syntax error.
    proc = _make_proc(2, stdout=b"broken.ts(1,18): error TS1005: ')' expected.")
    with (
        patch("qa.syntax_check._resolve_tsc", return_value=["tsc"]),
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
    ):
        result = await run_syntax_check(tmp_path, language="nodejs")

    assert not result.passed, "a pure-TS repo with a syntax error must FAIL the gate"
    assert "no .js files" not in (result.details or ""), "must not vacuous-pass"


@pytest.mark.asyncio
async def test_pure_ts_no_toolchain_degrades_loud(tmp_path: Path) -> None:
    """When .ts files exist but no TS compiler is resolvable, degrade LOUD.

    A missing toolchain is *unknown*, not *clean* (Phase-1B convention):
    passed=False with a ``skipped_toolchain_missing`` metric — never a vacuous
    'no .js files' pass.
    """
    _make_pure_ts_repo(tmp_path)
    with patch("qa.syntax_check._resolve_tsc", return_value=None):
        result = await run_syntax_check(tmp_path, language="nodejs")

    assert not result.passed
    assert result.metrics.get("skipped_toolchain_missing") is True
    assert "no .js files" not in (result.details or "")


@pytest.mark.asyncio
async def test_pure_ts_valid_passes(tmp_path: Path) -> None:
    """Valid TS (tsc returns 0) passes — no false positive."""
    (tmp_path / "package.json").write_text('{"name": "pure-ts"}\n')
    (tmp_path / "app.ts").write_text("const x: number = 1;\nconsole.log(x);\n")
    proc = _make_proc(0)
    with (
        patch("qa.syntax_check._resolve_tsc", return_value=["tsc"]),
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
    ):
        result = await run_syntax_check(tmp_path, language="nodejs")
    assert result.passed


@pytest.mark.asyncio
async def test_plain_js_still_checked_with_node(tmp_path: Path) -> None:
    """Regression guard: a .js syntax error is still caught via node --check."""
    (tmp_path / "package.json").write_text('{"name": "js"}\n')
    (tmp_path / "bad.js").write_text("function broken(\n")
    proc = _make_proc(1, stderr=b"SyntaxError: Unexpected end of input")
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await run_syntax_check(tmp_path, language="nodejs")
    assert not result.passed
    assert "SyntaxError" in (result.details or "")


# ---------------------------------------------------------------------------
# WS2-10: cargo clippy must include --workspace.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cargo_clippy_includes_workspace(tmp_path: Path) -> None:
    """RED-on-HEAD: clippy is invoked as ['cargo', 'clippy'] without --workspace."""
    proc = _make_proc(0)
    with patch(
        "asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
    ) as mock_exec:
        result = await run_lint(tmp_path, language="rust")
    assert result.passed
    args = list(mock_exec.call_args.args)
    assert args[0] == "cargo"
    assert "clippy" in args
    assert "--workspace" in args, "cargo clippy must lint the whole workspace"
