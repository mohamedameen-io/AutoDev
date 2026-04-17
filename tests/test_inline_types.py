"""Tests for inline_types: DelegationPendingSignal, InlineSuspendState,
InlineResponseFile, and InlineResponseError."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from adapters.inline_types import (
    DelegationPendingSignal,
    InlineResponseError,
    InlineResponseFile,
    InlineSuspendState,
)


class TestDelegationPendingSignal:
    def test_attributes_stored(self, tmp_path: Path) -> None:
        deleg_path = tmp_path / "1.1-developer.md"
        exc = DelegationPendingSignal("1.1", "developer", deleg_path)
        assert exc.task_id == "1.1"
        assert exc.role == "developer"
        assert exc.delegation_path == deleg_path

    def test_str_is_informative(self, tmp_path: Path) -> None:
        deleg_path = tmp_path / "2.3-reviewer.md"
        exc = DelegationPendingSignal("2.3", "reviewer", deleg_path)
        msg = str(exc)
        assert "2.3" in msg
        assert "reviewer" in msg
        assert str(deleg_path) in msg

    def test_is_exception_subclass(self, tmp_path: Path) -> None:
        exc = DelegationPendingSignal("1.1", "developer", tmp_path / "x.md")
        assert isinstance(exc, Exception)


class TestInlineSuspendState:
    def _minimal(self) -> dict:
        return {
            "session_id": "sess-abc",
            "suspended_at": "2026-04-16T10:00:00Z",
            "pending_task_id": "1.1",
            "pending_role": "developer",
            "delegation_path": ".autodev/delegations/1.1-developer.md",
            "response_path": ".autodev/responses/1.1-developer.json",
            "orchestrator_step": "developer",
        }

    def test_validates_minimal_fields(self) -> None:
        state = InlineSuspendState(**self._minimal())
        assert state.session_id == "sess-abc"
        assert state.pending_task_id == "1.1"
        assert state.retry_count == 0
        assert state.last_issues == []
        assert state.schema_version == "1.0"

    def test_rejects_unknown_fields(self) -> None:
        data = self._minimal()
        data["unknown_field"] = "oops"
        with pytest.raises(ValidationError):
            InlineSuspendState(**data)

    def test_rejects_invalid_orchestrator_step(self) -> None:
        data = self._minimal()
        data["orchestrator_step"] = "not_a_valid_step"
        with pytest.raises(ValidationError):
            InlineSuspendState(**data)

    def test_all_valid_orchestrator_steps(self) -> None:
        valid_steps = [
            "developer",
            "reviewer",
            "test_engineer",
            "critic_sounding_board",
            "plan_explorer",
            "plan_domain_expert",
            "plan_architect",
        ]
        for step in valid_steps:
            data = self._minimal()
            data["orchestrator_step"] = step
            state = InlineSuspendState(**data)
            assert state.orchestrator_step == step

    def test_optional_fields_populated(self) -> None:
        data = self._minimal()
        data["retry_count"] = 2
        data["last_issues"] = ["issue one", "issue two"]
        state = InlineSuspendState(**data)
        assert state.retry_count == 2
        assert state.last_issues == ["issue one", "issue two"]


class TestInlineResponseFile:
    def _minimal(self) -> dict:
        return {
            "task_id": "1.1",
            "role": "developer",
            "success": True,
            "text": "Implementation complete.",
        }

    def test_validates_minimal_fields(self) -> None:
        resp = InlineResponseFile(**self._minimal())
        assert resp.task_id == "1.1"
        assert resp.role == "developer"
        assert resp.success is True
        assert resp.text == "Implementation complete."
        assert resp.error is None
        assert resp.duration_s == 0.0
        assert resp.files_changed == []
        assert resp.diff is None
        assert resp.schema_version == "1.0"

    def test_rejects_unknown_fields(self) -> None:
        data = self._minimal()
        data["extra_key"] = "bad"
        with pytest.raises(ValidationError):
            InlineResponseFile(**data)

    def test_validates_all_fields_populated(self) -> None:
        resp = InlineResponseFile(
            task_id="2.1",
            role="reviewer",
            success=False,
            text="Review failed.",
            error="Missing tests",
            duration_s=3.14,
            files_changed=["src/foo.py", "src/bar.py"],
            diff="--- a/src/foo.py\n+++ b/src/foo.py\n",
        )
        assert resp.task_id == "2.1"
        assert resp.role == "reviewer"
        assert resp.success is False
        assert resp.error == "Missing tests"
        assert resp.duration_s == pytest.approx(3.14)
        assert resp.files_changed == ["src/foo.py", "src/bar.py"]
        assert resp.diff is not None


class TestInlineResponseError:
    def test_is_exception_subclass(self) -> None:
        err = InlineResponseError("something went wrong")
        assert isinstance(err, Exception)

    def test_message_preserved(self) -> None:
        err = InlineResponseError("response file missing")
        assert "response file missing" in str(err)
