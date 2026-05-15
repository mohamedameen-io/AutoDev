"""End-to-end tests that exercise the fake ``claude`` / ``cursor`` binaries.

These tests verify the *fake-binary protocol* (canned response lookup,
``AUTODEV_FAKE_FAILURE_MODE`` switches, prompt hashing) and a happy-path
flow through the real :class:`adapters.claude_code.ClaudeCodeAdapter` /
:class:`adapters.cursor.CursorAdapter` shelling out to the fakes via
``PATH``.

Marked ``@pytest.mark.integration`` so they can be excluded from the
fast unit-test loop.

Scope note (Phase 6 guardrail)
------------------------------
A fully parametrised orchestrator-level E2E (init → plan → execute → all
six tasks listed in the recovery plan) requires per-role prompt hashing
that is brittle against prompt template churn. The PR scope deliberately
keeps the Python side small: protocol coverage of the fakes themselves
plus one adapter-level happy-path call. The richer orchestrator scenarios
(empty_result visibility, max_turns escalation, worktree cleanup,
cursor usage-limit recovery) are tracked as a follow-up — the fakes
already support every failure mode they need.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterator

import pytest


# --- helpers -----------------------------------------------------------------


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FAKE_BIN_DIR = REPO_ROOT / "tests" / "fixtures" / "fake_binaries"
SAMPLE_PY = REPO_ROOT / "tests" / "fixtures" / "sample_project"
SAMPLE_TS = REPO_ROOT / "tests" / "fixtures" / "sample_project_ts"


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test"],
        cwd=str(repo),
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=str(repo),
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=str(repo), check=True)


@pytest.fixture
def fake_env(tmp_path: Path) -> Iterator[dict[str, Path]]:
    """Stage a sample project + fake binaries on PATH + a responses dir."""
    project = tmp_path / "project"
    shutil.copytree(SAMPLE_PY, project)
    _git_init(project)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for fname in ("fake-claude", "fake-cursor"):
        src = FAKE_BIN_DIR / fname
        # Real adapters spawn `claude` / `cursor` / `cursor-agent`. Symlink
        # the fakes under those names so a PATH-prepend redirects the spawn.
        target_name = "claude" if "claude" in fname else "cursor"
        dst = bin_dir / target_name
        shutil.copy2(src, dst)
        dst.chmod(0o755)
        if target_name == "cursor":
            # Adapter probes both `cursor` and `cursor-agent`.
            shutil.copy2(src, bin_dir / "cursor-agent")
            (bin_dir / "cursor-agent").chmod(0o755)

    responses = tmp_path / "responses"
    responses.mkdir()

    old_path = os.environ.get("PATH", "")
    old_resp = os.environ.get("AUTODEV_FAKE_RESPONSE_DIR")
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
    os.environ["AUTODEV_FAKE_RESPONSE_DIR"] = str(responses)
    try:
        yield {"project": project, "bin": bin_dir, "responses": responses}
    finally:
        os.environ["PATH"] = old_path
        if old_resp is None:
            os.environ.pop("AUTODEV_FAKE_RESPONSE_DIR", None)
        else:
            os.environ["AUTODEV_FAKE_RESPONSE_DIR"] = old_resp
        os.environ.pop("AUTODEV_FAKE_FAILURE_MODE", None)


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


# --- protocol coverage -------------------------------------------------------


@pytest.mark.integration
def test_fake_claude_default_response_is_valid_json(fake_env: dict[str, Path]) -> None:
    """Default fake-claude payload is parseable JSON with expected keys."""
    out = subprocess.check_output(
        ["claude", "-p", "hello", "--output-format", "json"],
        text=True,
    )
    parsed = json.loads(out)
    assert "result" in parsed
    assert "[fake-claude] default" in parsed["result"]


@pytest.mark.integration
def test_fake_cursor_default_response_is_valid_json(fake_env: dict[str, Path]) -> None:
    out = subprocess.check_output(
        ["cursor", "agent", "hello", "--print", "--output-format", "json", "--force"],
        text=True,
    )
    parsed = json.loads(out)
    assert "result" in parsed
    assert parsed["is_error"] is False


@pytest.mark.integration
def test_fake_claude_canned_response_lookup(fake_env: dict[str, Path]) -> None:
    """Drop a canned file, confirm fake serves it back verbatim."""
    prompt = "ping"
    canned = {"result": "PONG", "model": "fake", "stop_reason": "end_turn"}
    (fake_env["responses"] / f"response_{_md5(prompt)}.json").write_text(
        json.dumps(canned)
    )
    out = subprocess.check_output(["claude", "-p", prompt], text=True)
    assert json.loads(out) == canned


@pytest.mark.integration
def test_fake_claude_failure_mode_error_max_turns(fake_env: dict[str, Path]) -> None:
    env = os.environ.copy()
    env["AUTODEV_FAKE_FAILURE_MODE"] = "error_max_turns"
    proc = subprocess.run(
        ["claude", "-p", "anything"], capture_output=True, text=True, env=env
    )
    assert proc.returncode == 1
    parsed = json.loads(proc.stdout)
    assert parsed.get("subtype") == "error_max_turns"
    assert parsed.get("is_error") is True


@pytest.mark.integration
def test_fake_claude_failure_mode_empty_result(fake_env: dict[str, Path]) -> None:
    env = os.environ.copy()
    env["AUTODEV_FAKE_FAILURE_MODE"] = "empty_result"
    proc = subprocess.run(
        ["claude", "-p", "anything"], capture_output=True, text=True, env=env
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {"result": ""}


@pytest.mark.integration
def test_fake_cursor_failure_mode_usage_limit(fake_env: dict[str, Path]) -> None:
    env = os.environ.copy()
    env["AUTODEV_FAKE_FAILURE_MODE"] = "usage_limit"
    proc = subprocess.run(
        ["cursor", "agent", "anything", "--print"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 1
    assert "usage limit" in proc.stderr.lower()


@pytest.mark.integration
def test_fake_claude_nonzero_exit(fake_env: dict[str, Path]) -> None:
    env = os.environ.copy()
    env["AUTODEV_FAKE_FAILURE_MODE"] = "nonzero_exit"
    proc = subprocess.run(
        ["claude", "-p", "anything"], capture_output=True, text=True, env=env
    )
    assert proc.returncode == 3
    assert "synthetic failure" in proc.stderr


# --- adapter-level happy path ------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_claude_adapter_executes_against_fake_binary(
    fake_env: dict[str, Path],
) -> None:
    """ClaudeCodeAdapter shells out to the fake on PATH and parses JSON."""
    from adapters.claude_code import ClaudeCodeAdapter
    from adapters.types import AgentInvocation

    adapter = ClaudeCodeAdapter()
    inv = AgentInvocation(
        role="explorer",
        prompt="hello",
        cwd=fake_env["project"],
        timeout_s=10,
        max_turns=1,
    )
    result = await adapter.execute(inv)
    assert result.success
    assert "[fake-claude] default" in result.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cursor_adapter_executes_against_fake_binary(
    fake_env: dict[str, Path],
) -> None:
    """CursorAdapter shells out to the fake on PATH and parses JSON."""
    from adapters.cursor import CursorAdapter
    from adapters.types import AgentInvocation

    adapter = CursorAdapter()
    inv = AgentInvocation(
        role="explorer",
        prompt="hello",
        cwd=fake_env["project"],
        timeout_s=10,
    )
    result = await adapter.execute(inv)
    assert result.success
    assert "[fake-cursor] default" in result.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cursor_adapter_timeout_none_does_not_crash(
    fake_env: dict[str, Path],
) -> None:
    """Regression for v0.30.1 Bug F2 — Cursor adapter with timeout_s=None."""
    from adapters.cursor import CursorAdapter
    from adapters.types import AgentInvocation

    adapter = CursorAdapter()
    inv = AgentInvocation(
        role="reviewer",
        prompt="hello",
        cwd=fake_env["project"],
        timeout_s=None,  # <-- the bug
    )
    result = await adapter.execute(inv)
    # The fake returns instantly; the timeout=None path must not crash on
    # a NoneType format-string substitution.
    assert result.success
    assert "[fake-cursor] default" in result.text


# --- sample-project fixtures sanity ------------------------------------------


@pytest.mark.integration
def test_sample_python_project_is_runnable(fake_env: dict[str, Path]) -> None:
    project = fake_env["project"]
    assert (project / "main.py").exists()
    assert (project / "test_main.py").exists()
    assert (project / "spec.md").read_text().startswith("Add a `greet(name)`")


@pytest.mark.integration
def test_sample_ts_project_exists() -> None:
    assert (SAMPLE_TS / "index.ts").exists()
    assert (SAMPLE_TS / "package.json").exists()
    assert (SAMPLE_TS / "spec.md").exists()


# Suppress unused-import warning for asyncio when the file is parsed
# without the asyncio-mode plugin enabled.
_ = asyncio
