"""Tests for the InlineAdapter (file-based, ping-pong mode)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from adapters.inline import InlineAdapter
from adapters.inline_types import (
    DelegationPendingSignal,
    InlineResponseError,
)
from adapters.types import AgentInvocation, AgentResult
from state.paths import delegation_path, response_path


def _make_invocation(
    cwd: Path,
    *,
    role: str = "developer",
    task_id: str = "1.1",
    prompt: str = "Do the thing.",
    allowed_tools: list[str] | None = None,
    timeout_s: int = 600,
) -> AgentInvocation:
    return AgentInvocation(
        role=role,
        prompt=prompt,
        cwd=cwd,
        timeout_s=timeout_s,
        allowed_tools=allowed_tools,
        metadata={"task_id": task_id},
    )


def _make_response_json(
    task_id: str = "1.1",
    role: str = "developer",
    *,
    success: bool = True,
    text: str = "Done.",
    files_changed: list[str] | None = None,
    diff: str | None = None,
    error: str | None = None,
    duration_s: float = 1.5,
) -> str:
    return json.dumps(
        {
            "schema_version": "1.0",
            "task_id": task_id,
            "role": role,
            "success": success,
            "text": text,
            "error": error,
            "duration_s": duration_s,
            "files_changed": files_changed or [],
            "diff": diff,
        }
    )


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    # Initial commit so HEAD exists.
    (path / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=str(path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# 1. execute() writes delegation file and raises DelegationPendingSignal
# ---------------------------------------------------------------------------


def test_execute_writes_delegation_and_raises_signal(tmp_path: Path) -> None:
    adapter = InlineAdapter(cwd=tmp_path)
    inv = _make_invocation(tmp_path, role="developer", task_id="1.1")

    with pytest.raises(DelegationPendingSignal) as exc_info:
        import asyncio

        asyncio.get_event_loop().run_until_complete(adapter.execute(inv))

    sig = exc_info.value
    assert sig.task_id == "1.1"
    assert sig.role == "developer"
    assert sig.delegation_path.exists()
    assert sig.delegation_path == delegation_path(tmp_path, "1.1", "developer")


# ---------------------------------------------------------------------------
# 2. Delegation file has YAML frontmatter
# ---------------------------------------------------------------------------


def test_delegation_file_has_yaml_frontmatter(tmp_path: Path) -> None:
    adapter = InlineAdapter(cwd=tmp_path)
    inv = _make_invocation(tmp_path, role="reviewer", task_id="2.3")

    with pytest.raises(DelegationPendingSignal):
        import asyncio

        asyncio.get_event_loop().run_until_complete(adapter.execute(inv))

    del_path = delegation_path(tmp_path, "2.3", "reviewer")
    content = del_path.read_text(encoding="utf-8")

    assert content.startswith("---\n")
    assert 'task_id: "2.3"' in content
    assert 'role: "reviewer"' in content
    assert "response_path:" in content


# ---------------------------------------------------------------------------
# 3. Delegation file body contains the invocation prompt
# ---------------------------------------------------------------------------


def test_delegation_file_contains_prompt(tmp_path: Path) -> None:
    prompt = "Implement the feature described in the spec."
    adapter = InlineAdapter(cwd=tmp_path)
    inv = _make_invocation(tmp_path, prompt=prompt)

    with pytest.raises(DelegationPendingSignal):
        import asyncio

        asyncio.get_event_loop().run_until_complete(adapter.execute(inv))

    del_path = delegation_path(tmp_path, "1.1", "developer")
    content = del_path.read_text(encoding="utf-8")
    assert prompt in content


# ---------------------------------------------------------------------------
# 4. collect_response() returns AgentResult from valid JSON
# ---------------------------------------------------------------------------


def test_collect_response_returns_agent_result(tmp_path: Path) -> None:
    adapter = InlineAdapter(cwd=tmp_path)
    resp_path = response_path(tmp_path, "1.1", "developer")
    resp_path.parent.mkdir(parents=True, exist_ok=True)
    resp_path.write_text(
        _make_response_json("1.1", "developer", text="All done.", duration_s=2.0),
        encoding="utf-8",
    )

    result = adapter.collect_response("1.1", "developer")

    assert isinstance(result, AgentResult)
    assert result.success is True
    assert result.text == "All done."
    assert result.duration_s == 2.0
    assert result.error is None


# ---------------------------------------------------------------------------
# 5. collect_response() raises InlineResponseError if file missing
# ---------------------------------------------------------------------------


def test_collect_response_raises_if_missing(tmp_path: Path) -> None:
    adapter = InlineAdapter(cwd=tmp_path)

    with pytest.raises(InlineResponseError, match="response file not found"):
        adapter.collect_response("1.1", "developer")


# ---------------------------------------------------------------------------
# 6. collect_response() raises InlineResponseError on task_id/role mismatch
# ---------------------------------------------------------------------------


def test_collect_response_raises_if_mismatch(tmp_path: Path) -> None:
    adapter = InlineAdapter(cwd=tmp_path)
    resp_path = response_path(tmp_path, "1.1", "developer")
    resp_path.parent.mkdir(parents=True, exist_ok=True)
    # Write response for wrong role.
    resp_path.write_text(
        _make_response_json("1.1", "reviewer"),
        encoding="utf-8",
    )

    with pytest.raises(InlineResponseError, match="response mismatch"):
        adapter.collect_response("1.1", "developer")


# ---------------------------------------------------------------------------
# 7. collect_response() raises InlineResponseError on malformed JSON
# ---------------------------------------------------------------------------


def test_collect_response_raises_if_malformed_json(tmp_path: Path) -> None:
    adapter = InlineAdapter(cwd=tmp_path)
    resp_path = response_path(tmp_path, "1.1", "developer")
    resp_path.parent.mkdir(parents=True, exist_ok=True)
    resp_path.write_text("not valid json {{{{", encoding="utf-8")

    with pytest.raises(InlineResponseError, match="invalid response file"):
        adapter.collect_response("1.1", "developer")


# ---------------------------------------------------------------------------
# 8. collect_response() computes diff when response omits diff but has files
# ---------------------------------------------------------------------------


def test_collect_response_computes_diff_when_missing(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    # Modify a tracked file so git diff HEAD returns something.
    (tmp_path / "README.md").write_text("modified\n")

    adapter = InlineAdapter(cwd=tmp_path)
    resp_path = response_path(tmp_path, "1.1", "developer")
    resp_path.parent.mkdir(parents=True, exist_ok=True)
    resp_path.write_text(
        _make_response_json(
            "1.1",
            "developer",
            files_changed=["README.md"],
            diff=None,
        ),
        encoding="utf-8",
    )

    result = adapter.collect_response("1.1", "developer")

    # diff should be populated from git diff HEAD.
    assert result.diff is not None
    assert "README.md" in result.diff


# ---------------------------------------------------------------------------
# 9. has_pending_response() returns False before, True after writing
# ---------------------------------------------------------------------------


def test_has_pending_response(tmp_path: Path) -> None:
    adapter = InlineAdapter(cwd=tmp_path)

    assert adapter.has_pending_response("1.1", "developer") is False

    resp_path = response_path(tmp_path, "1.1", "developer")
    resp_path.parent.mkdir(parents=True, exist_ok=True)
    resp_path.write_text(_make_response_json(), encoding="utf-8")

    assert adapter.has_pending_response("1.1", "developer") is True


# ---------------------------------------------------------------------------
# 10. healthcheck() always returns (True, ...)
# ---------------------------------------------------------------------------


def test_healthcheck_always_healthy(tmp_path: Path) -> None:
    import asyncio

    adapter = InlineAdapter(cwd=tmp_path)
    ok, details = asyncio.get_event_loop().run_until_complete(adapter.healthcheck())

    assert ok is True
    assert isinstance(details, str)
    assert len(details) > 0


# ---------------------------------------------------------------------------
# 11. parallel() raises NotImplementedError
# ---------------------------------------------------------------------------


def test_parallel_raises_not_implemented(tmp_path: Path) -> None:
    import asyncio

    adapter = InlineAdapter(cwd=tmp_path)
    inv = _make_invocation(tmp_path)

    with pytest.raises(NotImplementedError, match="sequential"):
        asyncio.get_event_loop().run_until_complete(adapter.parallel([inv]))


# ---------------------------------------------------------------------------
# 12. init_workspace() doesn't crash
# ---------------------------------------------------------------------------


def test_init_workspace_logs(tmp_path: Path) -> None:
    import asyncio

    from adapters.types import AgentSpec

    adapter = InlineAdapter(cwd=tmp_path)
    specs = [
        AgentSpec(name="developer", description="writes code", prompt="You are a coder.")
    ]
    # Should not raise.
    asyncio.get_event_loop().run_until_complete(adapter.init_workspace(tmp_path, specs))


# ---------------------------------------------------------------------------
# 13. response_path() and delegation_path() return correct paths
# ---------------------------------------------------------------------------


def test_response_path_and_delegation_path(tmp_path: Path) -> None:
    adapter = InlineAdapter(cwd=tmp_path)

    expected_resp = response_path(tmp_path, "2.1", "reviewer")
    expected_del = delegation_path(tmp_path, "2.1", "reviewer")

    assert adapter.response_path("2.1", "reviewer") == expected_resp
    assert adapter.delegation_path("2.1", "reviewer") == expected_del


# ---------------------------------------------------------------------------
# 14. Delegation file response_path uses POSIX relative path
# ---------------------------------------------------------------------------


def test_delegation_file_response_path_is_relative(tmp_path: Path) -> None:
    adapter = InlineAdapter(cwd=tmp_path)
    inv = _make_invocation(tmp_path, role="developer", task_id="3.5")

    with pytest.raises(DelegationPendingSignal):
        import asyncio

        asyncio.get_event_loop().run_until_complete(adapter.execute(inv))

    del_path = delegation_path(tmp_path, "3.5", "developer")
    content = del_path.read_text(encoding="utf-8")

    # Extract the response_path value from frontmatter.
    for line in content.splitlines():
        if line.startswith("response_path:"):
            value = line.split(":", 1)[1].strip().strip('"')
            # Must be relative (not absolute).
            assert not value.startswith("/"), (
                f"response_path should be relative, got: {value}"
            )
            # Must use forward slashes (POSIX).
            assert "\\" not in value, (
                f"response_path must use POSIX separators, got: {value}"
            )
            break
    else:
        pytest.fail("response_path not found in delegation frontmatter")
