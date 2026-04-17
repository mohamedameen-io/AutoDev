"""Tests for :mod:`src.orchestrator.inline_state`."""

from __future__ import annotations

from pathlib import Path


from adapters.inline_types import InlineSuspendState
from orchestrator.inline_state import (
    clear_suspend_state,
    load_suspend_state,
    write_suspend_state,
)
from state.paths import inline_state_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(cwd: Path, **overrides: object) -> None:
    kwargs: dict = dict(
        cwd=cwd,
        session_id="sess-test",
        pending_task_id="1.1",
        pending_role="developer",
        delegation_path=cwd / ".autodev" / "delegations" / "1.1-developer.md",
        response_path=cwd / ".autodev" / "responses" / "1.1-developer.json",
        orchestrator_step="developer",
        retry_count=0,
        last_issues=[],
    )
    kwargs.update(overrides)
    write_suspend_state(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 1. write_suspend_state writes valid JSON
# ---------------------------------------------------------------------------


def test_write_suspend_state_creates_file(tmp_path: Path) -> None:
    _write(tmp_path)
    p = inline_state_path(tmp_path)
    assert p.exists()
    content = p.read_text(encoding="utf-8")
    assert '"schema_version"' in content
    assert '"1.1"' in content
    assert '"developer"' in content


# ---------------------------------------------------------------------------
# 2. load_suspend_state returns None when no file exists
# ---------------------------------------------------------------------------


def test_load_suspend_state_returns_none_when_missing(tmp_path: Path) -> None:
    result = load_suspend_state(tmp_path)
    assert result is None


# ---------------------------------------------------------------------------
# 3. load_suspend_state returns InlineSuspendState after write
# ---------------------------------------------------------------------------


def test_load_suspend_state_returns_state_after_write(tmp_path: Path) -> None:
    _write(
        tmp_path,
        pending_task_id="2.3",
        pending_role="reviewer",
        orchestrator_step="reviewer",
    )
    state = load_suspend_state(tmp_path)
    assert state is not None
    assert isinstance(state, InlineSuspendState)
    assert state.pending_task_id == "2.3"
    assert state.pending_role == "reviewer"
    assert state.orchestrator_step == "reviewer"
    assert state.session_id == "sess-test"


# ---------------------------------------------------------------------------
# 4. clear_suspend_state removes the file
# ---------------------------------------------------------------------------


def test_clear_suspend_state_removes_file(tmp_path: Path) -> None:
    _write(tmp_path)
    assert inline_state_path(tmp_path).exists()
    clear_suspend_state(tmp_path)
    assert not inline_state_path(tmp_path).exists()


# ---------------------------------------------------------------------------
# 5. clear_suspend_state is idempotent (no error if file missing)
# ---------------------------------------------------------------------------


def test_clear_suspend_state_idempotent(tmp_path: Path) -> None:
    # Should not raise even when file doesn't exist.
    clear_suspend_state(tmp_path)


# ---------------------------------------------------------------------------
# 6. Round-trip: write → load → clear → load returns None
# ---------------------------------------------------------------------------


def test_round_trip(tmp_path: Path) -> None:
    _write(tmp_path, retry_count=2, last_issues=["lint failed", "tests failed"])
    state = load_suspend_state(tmp_path)
    assert state is not None
    assert state.retry_count == 2
    assert state.last_issues == ["lint failed", "tests failed"]

    clear_suspend_state(tmp_path)
    assert load_suspend_state(tmp_path) is None


# ---------------------------------------------------------------------------
# 7. delegation_path stored as relative when inside cwd
# ---------------------------------------------------------------------------


def test_delegation_path_stored_relative(tmp_path: Path) -> None:
    del_path = tmp_path / ".autodev" / "delegations" / "1.1-developer.md"
    resp_path = tmp_path / ".autodev" / "responses" / "1.1-developer.json"
    write_suspend_state(
        cwd=tmp_path,
        session_id="s",
        pending_task_id="1.1",
        pending_role="developer",
        delegation_path=del_path,
        response_path=resp_path,
        orchestrator_step="developer",
    )
    state = load_suspend_state(tmp_path)
    assert state is not None
    # Should be relative, not absolute.
    assert not state.delegation_path.startswith("/")
    assert ".autodev/delegations/1.1-developer.md" in state.delegation_path


# ---------------------------------------------------------------------------
# 8. write_suspend_state creates parent directory if missing
# ---------------------------------------------------------------------------


def test_write_creates_parent_dir(tmp_path: Path) -> None:
    # tmp_path has no .autodev dir yet.
    assert not (tmp_path / ".autodev").exists()
    _write(tmp_path)
    assert inline_state_path(tmp_path).exists()
