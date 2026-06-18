"""Engagement-first gate tests for WS2-9 (tsc guard) and WS2-10 (cargo --workspace).

These tests pin two false-block fixes in :mod:`qa.build_check`:

* **WS2-9** — ``npx tsc --noEmit`` must NOT run (and must not block) for a TS/JS
  repo that has *no* ``tsconfig.json``. Running ``tsc`` with no config compiles
  every ``.js`` it can find and fails on perfectly valid JS-only repos, so the
  gate falsely blocks. When a ``tsconfig.json`` IS present, ``tsc`` must run, and
  it must resolve from ``node_modules/.bin`` before falling back to ``npx``.
* **WS2-10** — ``cargo check`` on a *virtual* workspace manifest (a ``Cargo.toml``
  with ``[workspace]`` and no ``[package]``) errors without ``--workspace``, so the
  invocation must carry ``--workspace``.

RED-on-HEAD: the no-tsconfig case false-blocks (tsc runs and is reported as a
build failure) and the cargo command lacks ``--workspace``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qa.build_check import run_build_check


def _make_proc(returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


# ---------------------------------------------------------------------------
# WS2-9: tsc guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ts_repo_without_tsconfig_does_not_false_block(tmp_path: Path) -> None:
    """No tsconfig.json → tsc must be skipped, not run-and-fail.

    On HEAD this RED-fails: with no build script and no tsconfig, the code runs
    ``npx tsc --noEmit`` which would here exit non-zero (simulated build
    failure), producing ``passed=False`` — a false block on a config-less repo.
    """
    import json

    (tmp_path / "package.json").write_text(json.dumps({"scripts": {}}))
    # Simulate tsc exiting non-zero (what config-less tsc does on a JS repo).
    proc = _make_proc(1, stderr=b"error TS18003: No inputs were found")
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
        result = await run_build_check(tmp_path, language="nodejs")
    # Must NOT false-block: no tsconfig means the tsc step is skipped entirely.
    assert result.passed, f"config-less TS repo false-blocked: {result.details!r}"
    assert "tsc" not in result.details.lower() or "skip" in result.details.lower()
    # tsc must not have been invoked at all.
    if mock_exec.call_args is not None:
        assert mock_exec.call_args.args[0] != "npx"


@pytest.mark.asyncio
async def test_ts_repo_with_tsconfig_runs_tsc(tmp_path: Path) -> None:
    """tsconfig.json present → tsc must actually run."""
    import json

    (tmp_path / "package.json").write_text(json.dumps({"scripts": {}}))
    (tmp_path / "tsconfig.json").write_text("{}")
    proc = _make_proc(0)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
        result = await run_build_check(tmp_path, language="nodejs")
    assert result.passed
    args = mock_exec.call_args.args
    # tsc runs: either via npx tsc or via node_modules/.bin/tsc.
    joined = " ".join(args)
    assert "tsc" in joined
    assert "--noEmit" in joined


@pytest.mark.asyncio
async def test_ts_repo_with_tsconfig_prefers_local_tsc(tmp_path: Path) -> None:
    """When node_modules/.bin/tsc exists it is resolved before npx."""
    import json

    (tmp_path / "package.json").write_text(json.dumps({"scripts": {}}))
    (tmp_path / "tsconfig.json").write_text("{}")
    local_bin = tmp_path / "node_modules" / ".bin"
    local_bin.mkdir(parents=True)
    local_tsc = local_bin / "tsc"
    local_tsc.write_text("#!/bin/sh\n")
    local_tsc.chmod(0o755)

    proc = _make_proc(0)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
        result = await run_build_check(tmp_path, language="nodejs")
    assert result.passed
    # First arg must be the local tsc binary, not npx.
    assert mock_exec.call_args.args[0] == str(local_tsc)


# ---------------------------------------------------------------------------
# WS2-10: cargo --workspace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cargo_workspace_passes_workspace_flag(tmp_path: Path) -> None:
    """Virtual workspace manifest → cargo invocation carries --workspace.

    RED-on-HEAD: today's command is just ``cargo check`` with no ``--workspace``.
    """
    (tmp_path / "Cargo.toml").write_text("[workspace]\nmembers = [\"a\"]\n")
    proc = _make_proc(0)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
        result = await run_build_check(tmp_path, language="rust")
    assert result.passed
    args = list(mock_exec.call_args.args)
    assert args[0] == "cargo"
    assert "--workspace" in args, f"cargo missing --workspace: {args!r}"


@pytest.mark.asyncio
async def test_cargo_non_workspace_still_works(tmp_path: Path) -> None:
    """A plain package manifest still builds (regression guard)."""
    (tmp_path / "Cargo.toml").write_text("[package]\nname = \"x\"\nversion = \"0.1.0\"\n")
    proc = _make_proc(0)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
        result = await run_build_check(tmp_path, language="rust")
    assert result.passed
    assert mock_exec.call_args.args[0] == "cargo"
