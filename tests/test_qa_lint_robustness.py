"""Lint-gate robustness tests.

Three cases:
  (a) No eslint config present → skip (passed=True, skipped_lint_no_config metric).
  (b) Tool setup/env error (ENOENT, "couldn't find config") → warn (non-blocking).
  (c) Genuine lint violation → block (passed=False, severity=="block").
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qa.lint import run_lint


def _make_proc(returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


# ---------------------------------------------------------------------------
# Case (a): no eslint config → skip (passed=True, skipped_lint_no_config=True)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eslint_no_config_skips(tmp_path: Path) -> None:
    """When no eslint config exists in cwd, the gate should skip (passed=True)
    with a ``skipped_lint_no_config`` metric rather than blocking the pipeline."""
    # tmp_path is empty — no eslint.config.js / .eslintrc* / package.json
    result = await run_lint(tmp_path, language="nodejs")
    assert result.passed, (
        f"Expected skip (passed=True) for no-config repo, got passed=False. "
        f"Details: {result.details!r}"
    )
    assert result.metrics.get("skipped_lint_no_config"), (
        f"Expected skipped_lint_no_config metric, got metrics={result.metrics!r}"
    )


@pytest.mark.asyncio
async def test_eslint_no_config_does_not_call_subprocess(tmp_path: Path) -> None:
    """No-config skip must not invoke eslint at all."""
    with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
        result = await run_lint(tmp_path, language="nodejs")
    assert result.passed
    mock_exec.assert_not_called()


@pytest.mark.asyncio
async def test_eslint_with_eslintrc_json_runs(tmp_path: Path) -> None:
    """When a .eslintrc.json exists the gate proceeds normally (no skip)."""
    (tmp_path / ".eslintrc.json").write_text("{}", encoding="utf-8")
    proc = _make_proc(0)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
        result = await run_lint(tmp_path, language="nodejs")
    assert result.passed
    mock_exec.assert_called_once()


@pytest.mark.asyncio
async def test_eslint_with_eslint_config_js_runs(tmp_path: Path) -> None:
    """When an eslint.config.js exists the gate proceeds normally."""
    (tmp_path / "eslint.config.js").write_text("export default [];\n", encoding="utf-8")
    proc = _make_proc(0)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
        result = await run_lint(tmp_path, language="nodejs")
    assert result.passed
    mock_exec.assert_called_once()


@pytest.mark.asyncio
async def test_eslint_with_eslint_config_ts_runs(tmp_path: Path) -> None:
    """A repo whose ONLY config is eslint.config.ts must NOT be skipped as no-config.

    ESLint 9.10+ supports TypeScript flat configs (eslint.config.{ts,mts,cts}); a
    repo using only eslint.config.ts has a real config and should proceed to lint,
    not hit the no-config skip.
    """
    (tmp_path / "eslint.config.ts").write_text("export default [];\n", encoding="utf-8")
    proc = _make_proc(0)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
        result = await run_lint(tmp_path, language="nodejs")
    assert result.passed
    assert not result.metrics.get("skipped_lint_no_config"), (
        "eslint.config.ts is a valid config; the no-config skip must NOT fire. "
        f"Got metrics={result.metrics!r}"
    )
    mock_exec.assert_called_once()


@pytest.mark.asyncio
async def test_eslint_with_package_json_eslint_config_runs(tmp_path: Path) -> None:
    """When package.json contains an 'eslintConfig' key the gate runs."""
    (tmp_path / "package.json").write_text('{"eslintConfig": {}}', encoding="utf-8")
    proc = _make_proc(0)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
        result = await run_lint(tmp_path, language="nodejs")
    assert result.passed
    mock_exec.assert_called_once()


@pytest.mark.asyncio
async def test_eslint_package_json_without_eslint_config_skips(tmp_path: Path) -> None:
    """package.json without 'eslintConfig' key should still skip."""
    (tmp_path / "package.json").write_text('{"name": "foo"}', encoding="utf-8")
    result = await run_lint(tmp_path, language="nodejs")
    assert result.passed
    assert result.metrics.get("skipped_lint_no_config")


# ---------------------------------------------------------------------------
# Case (b): tool setup/env error → warn (non-blocking, pipeline continues)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eslint_env_error_enoent_is_warn(tmp_path: Path) -> None:
    """ENOENT (npx not found / eslint binary missing) → severity='warn', non-blocking.

    The consumer at execute_phase dispatches: passed=False + severity==block → halt.
    For setup errors we want passed=True + severity==warn so it surfaces as a
    non-blocking warning instead of halting the pipeline.
    """
    (tmp_path / "eslint.config.js").write_text("export default [];\n", encoding="utf-8")
    with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
        result = await run_lint(tmp_path, language="nodejs")
    # Must NOT block the pipeline — passes as a non-blocking advisory.
    assert result.passed, (
        "ESLint ENOENT should not produce a blocking failure; "
        f"got passed={result.passed!r}, severity={result.severity!r}"
    )
    # Should surface as warn (non-blocking)
    assert result.severity == "warn", (
        f"Expected severity='warn' for ENOENT, got {result.severity!r}"
    )


@pytest.mark.asyncio
async def test_eslint_config_not_found_stderr_is_warn(tmp_path: Path) -> None:
    """ESLint exit non-zero with 'couldn't find' in stderr → severity='warn', not block."""
    (tmp_path / "eslint.config.js").write_text("export default [];\n", encoding="utf-8")
    stderr_msg = b"Error: Could not find the config file. Please check your ESLint configuration."
    proc = _make_proc(1, stderr=stderr_msg)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await run_lint(tmp_path, language="nodejs")
    assert result.passed, (
        "Config-not-found error should not block; "
        f"got passed={result.passed!r}, severity={result.severity!r}"
    )
    assert result.severity == "warn", (
        f"Expected severity='warn' for config-error, got {result.severity!r}"
    )


@pytest.mark.asyncio
async def test_eslint_startup_couldnt_find_configuration_file_is_warn(tmp_path: Path) -> None:
    """The real ESLint startup error 'couldn't find a configuration file' → warn (non-blocking)."""
    (tmp_path / "eslint.config.js").write_text("export default [];\n", encoding="utf-8")
    stderr_msg = b"Oops! Something went wrong! ESLint couldn't find a configuration file."
    proc = _make_proc(2, stderr=stderr_msg)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await run_lint(tmp_path, language="nodejs")
    assert result.passed, (
        f"Startup config-not-found should not block, got passed={result.passed!r}"
    )
    assert result.severity == "warn", (
        f"Expected severity='warn' for startup config error, got {result.severity!r}"
    )


@pytest.mark.asyncio
async def test_eslint_v9_couldnt_find_eslint_config_file_is_warn(tmp_path: Path) -> None:
    """The exact ESLint v9 phrase 'couldn't find an eslint.config.(js|mjs|cjs) file' → warn."""
    (tmp_path / "eslint.config.js").write_text("export default [];\n", encoding="utf-8")
    stderr_msg = (
        b"Oops! Something went wrong! :(\n\n"
        b"ESLint couldn't find an eslint.config.(js|mjs|cjs) file.\n\n"
        b"From ESLint v9.0.0, the default configuration file is now eslint.config.js."
    )
    proc = _make_proc(2, stderr=stderr_msg)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await run_lint(tmp_path, language="nodejs")
    assert result.passed, (
        f"ESLint v9 config-not-found should not block, got passed={result.passed!r}"
    )
    assert result.severity == "warn", (
        f"Expected severity='warn' for ESLint v9 config error, got {result.severity!r}"
    )


# ---------------------------------------------------------------------------
# Case (c): genuine lint violation → block (passed=False, severity=="block")
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eslint_real_violation_blocks(tmp_path: Path) -> None:
    """A genuine lint violation (non-zero exit + violation output) → severity='block'."""
    (tmp_path / "eslint.config.js").write_text("export default [];\n", encoding="utf-8")
    violation_output = b"src/index.js\n  1:1  error  'foo' is not defined  no-undef\n\n1 problem (1 error, 0 warnings)"
    proc = _make_proc(1, stderr=violation_output)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await run_lint(tmp_path, language="nodejs")
    assert not result.passed, (
        f"Genuine violation should fail (passed=False), got passed={result.passed!r}"
    )
    assert result.severity == "block", (
        f"Genuine violation should severity='block', got {result.severity!r}"
    )


@pytest.mark.asyncio
async def test_eslint_real_violation_exit_1_no_config_error_blocks(tmp_path: Path) -> None:
    """Exit=1 without any config-error keywords stays 'block'."""
    (tmp_path / ".eslintrc.json").write_text("{}", encoding="utf-8")
    proc = _make_proc(1, stderr=b"no-unused-vars: 'x' is assigned a value but never used")
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await run_lint(tmp_path, language="nodejs")
    assert not result.passed
    assert result.severity == "block"


@pytest.mark.asyncio
async def test_eslint_violation_advisory_mentioning_configuration_file_stays_block(
    tmp_path: Path,
) -> None:
    """Regression: a GENUINE violation whose message merely CONTAINS the substring
    'configuration file' (a rule advisory, not a startup error) must stay
    passed=False AND severity=='block' — it must NOT be demoted to warn.

    This pins the narrowing of _ESLINT_SETUP_ERROR_SIGNALS: bare 'configuration
    file' is no longer a setup-error signal, so a real lint failure that happens
    to advise editing your configuration file is never reclassified as
    non-blocking.
    """
    (tmp_path / "eslint.config.js").write_text("export default [];\n", encoding="utf-8")
    # Real no-undef violation; the advisory text contains "configuration file"
    # but there is NO startup config-not-found error here.
    violation_output = (
        b"src/index.js\n"
        b"  1:1  error  'process' is not defined  no-undef\n\n"
        b"  Consider adding 'env: { node: true }' to your configuration file.\n\n"
        b"1 problem (1 error, 0 warnings)"
    )
    proc = _make_proc(1, stderr=violation_output)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await run_lint(tmp_path, language="nodejs")
    assert not result.passed, (
        "A genuine violation that mentions 'configuration file' must fail "
        f"(passed=False), got passed={result.passed!r}. Details: {result.details!r}"
    )
    assert result.severity == "block", (
        "A genuine violation mentioning 'configuration file' must stay block, "
        f"got severity={result.severity!r}. Details: {result.details!r}"
    )
    assert not result.metrics.get("lint_setup_error"), (
        "Genuine violation must NOT be flagged lint_setup_error; "
        f"got metrics={result.metrics!r}"
    )
