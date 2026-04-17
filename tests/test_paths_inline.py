"""Tests for inline adapter path helpers added to src.state.paths."""

from __future__ import annotations

from pathlib import Path

from state.paths import (
    AUTODEV_DIR,
    DELEGATIONS_DIR,
    INLINE_STATE_FILE,
    RESPONSES_DIR,
    delegation_path,
    delegations_dir,
    inline_state_path,
    response_path,
    responses_dir,
)


class TestDelegationsDir:
    def test_returns_correct_path(self, tmp_path: Path) -> None:
        result = delegations_dir(tmp_path)
        assert result == tmp_path / AUTODEV_DIR / DELEGATIONS_DIR

    def test_is_path_instance(self, tmp_path: Path) -> None:
        assert isinstance(delegations_dir(tmp_path), Path)


class TestDelegationPath:
    def test_returns_correct_path(self, tmp_path: Path) -> None:
        result = delegation_path(tmp_path, "1.1", "developer")
        expected = tmp_path / AUTODEV_DIR / DELEGATIONS_DIR / "1.1-developer.md"
        assert result == expected

    def test_different_task_and_role(self, tmp_path: Path) -> None:
        result = delegation_path(tmp_path, "3.2", "reviewer")
        expected = tmp_path / AUTODEV_DIR / DELEGATIONS_DIR / "3.2-reviewer.md"
        assert result == expected

    def test_filename_format(self, tmp_path: Path) -> None:
        result = delegation_path(tmp_path, "2.5", "test_engineer")
        assert result.name == "2.5-test_engineer.md"
        assert result.suffix == ".md"


class TestResponsesDir:
    def test_returns_correct_path(self, tmp_path: Path) -> None:
        result = responses_dir(tmp_path)
        assert result == tmp_path / AUTODEV_DIR / RESPONSES_DIR

    def test_is_path_instance(self, tmp_path: Path) -> None:
        assert isinstance(responses_dir(tmp_path), Path)


class TestResponsePath:
    def test_returns_correct_path(self, tmp_path: Path) -> None:
        result = response_path(tmp_path, "1.1", "developer")
        expected = tmp_path / AUTODEV_DIR / RESPONSES_DIR / "1.1-developer.json"
        assert result == expected

    def test_different_task_and_role(self, tmp_path: Path) -> None:
        result = response_path(tmp_path, "4.1", "critic_sounding_board")
        expected = (
            tmp_path / AUTODEV_DIR / RESPONSES_DIR / "4.1-critic_sounding_board.json"
        )
        assert result == expected

    def test_filename_format(self, tmp_path: Path) -> None:
        result = response_path(tmp_path, "2.3", "reviewer")
        assert result.name == "2.3-reviewer.json"
        assert result.suffix == ".json"


class TestInlineStatePath:
    def test_returns_correct_path(self, tmp_path: Path) -> None:
        result = inline_state_path(tmp_path)
        assert result == tmp_path / AUTODEV_DIR / INLINE_STATE_FILE

    def test_filename(self, tmp_path: Path) -> None:
        result = inline_state_path(tmp_path)
        assert result.name == "inline-state.json"

    def test_is_path_instance(self, tmp_path: Path) -> None:
        assert isinstance(inline_state_path(tmp_path), Path)
