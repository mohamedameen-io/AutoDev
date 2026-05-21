"""v0.36.0 G1: spec validator front-gate tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from cli import cli
from orchestrator.spec_validator import (
    SpecValidationResult,
    validate_spec,
    validate_spec_text,
)


_WELL_FORMED_BUG_MD = """\
# Bug: rendering glitch on dashboard

The dashboard widget crashes when the user clicks the refresh button
during a hot reload.

## Expected
The widget should refresh cleanly without crashing.

## Acceptance
- [ ] Crash no longer reproduces.
- [ ] Unit test covers the regression.
"""


def test_validate_spec_passes_for_well_formed_bug_md(tmp_path: Path) -> None:
    spec_path = tmp_path / "bug.md"
    spec_path.write_text(_WELL_FORMED_BUG_MD)
    result = validate_spec(spec_path)
    assert result.ok is True
    assert result.reasons == ()


def test_validate_spec_rejects_empty(tmp_path: Path) -> None:
    spec_path = tmp_path / "empty.md"
    spec_path.write_text("")
    result = validate_spec(spec_path)
    assert result.ok is False
    assert "spec_empty" in result.reasons


def test_validate_spec_rejects_missing_file(tmp_path: Path) -> None:
    result = validate_spec(tmp_path / "does-not-exist.md")
    assert result.ok is False
    assert "spec_missing" in result.reasons


def test_validate_spec_rejects_one_liner(tmp_path: Path) -> None:
    spec_path = tmp_path / "one.md"
    # 12 chars — well below the 40-char floor.
    spec_path.write_text("fix the bug\n")
    result = validate_spec(spec_path)
    assert result.ok is False
    assert "spec_too_short" in result.reasons


def test_validate_spec_rejects_missing_scope(tmp_path: Path) -> None:
    spec_path = tmp_path / "no_scope.md"
    # Short one-liner (< 80 chars) without any scope marker token.
    spec_path.write_text("please look at the thing\n")
    result = validate_spec(spec_path)
    assert result.ok is False
    assert "spec_no_scope_markers" in result.reasons


def test_validate_spec_rejects_missing_acceptance_signal(tmp_path: Path) -> None:
    spec_path = tmp_path / "no_accept.md"
    # Long enough, has scope marker (fix), but no acceptance signal.
    spec_path.write_text(
        "Fix the dashboard widget so it renders the right colors and "
        "labels everywhere across the app.\n"
    )
    result = validate_spec(spec_path)
    assert result.ok is False
    assert "spec_no_acceptance_signal" in result.reasons


def test_validate_spec_text_round_trips_with_spec_body() -> None:
    """The text-based helper matches the file-based helper output."""
    result = validate_spec_text(_WELL_FORMED_BUG_MD)
    assert result.ok is True


def test_validate_spec_text_empty_rejected() -> None:
    result = validate_spec_text("")
    assert result.ok is False
    assert "spec_empty" in result.reasons


# ---------------------------------------------------------------------------
# CLI plumbing — `autodev plan <intent>` short-circuits with exit 4.
# ---------------------------------------------------------------------------


def _write_min_cfg(cwd: Path) -> None:
    from config.defaults import default_config
    from config.loader import save_config

    autodev_dir = cwd / ".autodev"
    autodev_dir.mkdir(parents=True, exist_ok=True)
    cfg = default_config()
    save_config(cfg, autodev_dir / "config.json")


def test_plan_command_short_circuits_on_invalid_spec(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_min_cfg(cwd)
        result = runner.invoke(cli, ["plan", "fix"])
        assert result.exit_code == 4, result.output
        assert "spec rejected" in result.output


def test_plan_command_skip_spec_validation_flag_bypasses_check(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        _write_min_cfg(cwd)
        with (
            patch("cli.commands.plan.get_adapter") as mock_get_adapter,
            patch("cli.commands.plan.Orchestrator") as mock_orch_cls,
        ):
            # v0.38.0 HK10: get_adapter returns (adapter, selection_meta).
            mock_get_adapter.return_value = (MagicMock(), {"platform": "claude_code"})
            mock_orch = MagicMock()
            # Plan return value must walk like a Plan; the rendering
            # helper accesses ``.metadata``, ``.plan_id``, ``.phases``.
            mock_plan = MagicMock()
            mock_plan.metadata = {"title": "stub"}
            mock_plan.plan_id = "stub-id"
            mock_plan.phases = []
            mock_orch.plan = AsyncMock(return_value=mock_plan)
            mock_orch_cls.return_value = mock_orch
            result = runner.invoke(
                cli,
                ["plan", "fix", "--skip-spec-validation"],
                catch_exceptions=False,
            )
            assert result.exit_code == 0, result.output


def test_validate_spec_result_is_frozen_dataclass() -> None:
    """Sanity: the result dataclass is frozen so callers can stash it."""
    r = SpecValidationResult(ok=True, reasons=())
    with pytest.raises(Exception):
        r.ok = False  # type: ignore[misc]
