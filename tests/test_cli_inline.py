"""CLI smoke tests for inline (agent-embedded) mode — Phase E."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from adapters.inline_types import DelegationPendingSignal
from cli import cli
from config.defaults import default_config
from config.loader import save_config
from orchestrator.inline_state import write_suspend_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(cwd: Path, platform: str = "inline") -> None:
    """Write a minimal valid config.json into <cwd>/.autodev/."""
    cfg = default_config()
    cfg.platform = platform  # type: ignore[assignment]
    autodev_dir = cwd / ".autodev"
    autodev_dir.mkdir(parents=True, exist_ok=True)
    save_config(cfg, autodev_dir / "config.json")


def _write_inline_state(
    cwd: Path, task_id: str = "1.1", role: str = "developer"
) -> None:
    """Write a minimal inline-state.json into <cwd>/.autodev/."""
    write_suspend_state(
        cwd=cwd,
        session_id="sess-test",
        pending_task_id=task_id,
        pending_role=role,
        delegation_path=cwd / ".autodev" / "delegations" / f"{task_id}-{role}.md",
        response_path=cwd / ".autodev" / "responses" / f"{task_id}-{role}.json",
        orchestrator_step="developer",
    )


def _write_response_file(
    cwd: Path, task_id: str = "1.1", role: str = "developer"
) -> None:
    """Write a valid response JSON file so has_pending_response() returns True."""
    resp_dir = cwd / ".autodev" / "responses"
    resp_dir.mkdir(parents=True, exist_ok=True)
    resp_path = resp_dir / f"{task_id}-{role}.json"
    resp_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "task_id": task_id,
                "role": role,
                "success": True,
                "text": "done",
                "error": None,
                "duration_s": 1.0,
                "files_changed": [],
                "diff": None,
            }
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Test 1: execute exits 0 when DelegationPendingSignal is raised
# ---------------------------------------------------------------------------


def test_execute_inline_mode_exits_cleanly(tmp_path: Path) -> None:
    """InlineAdapter raises DelegationPendingSignal; CLI exits 0 with delegation message."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd, platform="inline")

        delegation_file = cwd / ".autodev" / "delegations" / "1.1-developer.md"
        delegation_file.parent.mkdir(parents=True, exist_ok=True)
        delegation_file.touch()

        sig = DelegationPendingSignal(
            task_id="1.1",
            role="developer",
            delegation_path=delegation_file,
        )

        with (
            patch("cli.commands.execute.get_adapter") as mock_get_adapter,
            patch("cli.commands.execute.Orchestrator") as mock_orch_cls,
        ):
            mock_adapter = MagicMock()
            mock_get_adapter.return_value = mock_adapter

            mock_orch = MagicMock()
            mock_orch.execute = AsyncMock(side_effect=sig)
            mock_orch_cls.return_value = mock_orch

            result = runner.invoke(cli, ["execute"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert "Delegation written" in result.output
    assert "autodev resume" in result.output


# ---------------------------------------------------------------------------
# Test 2: resume exits 0 when inline state exists but no response yet
# ---------------------------------------------------------------------------


def test_resume_inline_waits_for_response(tmp_path: Path) -> None:
    """inline-state.json present but no response file → exit 0 with waiting message."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd, platform="inline")
        _write_inline_state(cwd)
        # Deliberately do NOT write a response file.

        result = runner.invoke(cli, ["resume"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert "Waiting for agent response" in result.output
    assert "1.1-developer" in result.output


# ---------------------------------------------------------------------------
# Test 3: resume continues when inline state + response file both exist
# ---------------------------------------------------------------------------


def test_resume_inline_continues_with_response(tmp_path: Path) -> None:
    """inline-state.json + response file present → orchestrator.resume() is called."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_config(cwd, platform="inline")
        _write_inline_state(cwd)
        _write_response_file(cwd)

        with patch("cli.commands.resume.Orchestrator") as mock_orch_cls:
            mock_orch = MagicMock()
            mock_orch.resume = AsyncMock(return_value=[])
            mock_orch_cls.return_value = mock_orch

            result = runner.invoke(cli, ["resume"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    mock_orch.resume.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 4: init --inline sets platform="inline" in config.json
# ---------------------------------------------------------------------------


def test_init_inline_flag_sets_platform(tmp_path: Path) -> None:
    """``autodev init --inline`` writes platform='inline' to config.json."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)

        result = runner.invoke(cli, ["init", "--inline"], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        config_file = cwd / ".autodev" / "config.json"
        assert config_file.exists(), "config.json was not created"
        config_data = json.loads(config_file.read_text(encoding="utf-8"))
        assert config_data["platform"] == "inline"
