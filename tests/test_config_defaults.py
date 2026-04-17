"""Tests for :mod:`config.defaults` -- resolve_model and default_config."""

from __future__ import annotations

from config.defaults import default_config, resolve_model


# ---------------------------------------------------------------------------
# resolve_model: explicit model always wins
# ---------------------------------------------------------------------------


def test_resolve_model_explicit_model_returned() -> None:
    """When an explicit model is provided it is returned unchanged."""
    assert resolve_model("gpt-4", role="architect", platform="cursor") == "gpt-4"


# ---------------------------------------------------------------------------
# resolve_model: Cursor platform mapping
# ---------------------------------------------------------------------------


def test_resolve_model_cursor_architect() -> None:
    assert resolve_model(None, role="architect", platform="cursor") == "opus"


def test_resolve_model_cursor_architect_b() -> None:
    assert resolve_model(None, role="architect_b", platform="cursor") == "opus"


def test_resolve_model_cursor_reviewer() -> None:
    assert resolve_model(None, role="reviewer", platform="cursor") == "sonnet"


def test_resolve_model_cursor_judge() -> None:
    assert resolve_model(None, role="judge", platform="cursor") == "sonnet"


def test_resolve_model_cursor_developer() -> None:
    """Cursor developer falls through to the catch-all 'auto'."""
    assert resolve_model(None, role="developer", platform="cursor") == "auto"


def test_resolve_model_cursor_explorer() -> None:
    """Cursor explorer falls through to the catch-all 'auto'."""
    assert resolve_model(None, role="explorer", platform="cursor") == "auto"


# ---------------------------------------------------------------------------
# resolve_model: Claude Code (platform="auto") mapping
# ---------------------------------------------------------------------------


def test_resolve_model_claude_architect() -> None:
    assert resolve_model(None, role="architect", platform="auto") == "opus"


def test_resolve_model_claude_explorer() -> None:
    assert resolve_model(None, role="explorer", platform="auto") == "haiku"


def test_resolve_model_claude_developer() -> None:
    """Non-architect, non-explorer roles default to sonnet on auto platform."""
    assert resolve_model(None, role="developer", platform="auto") == "sonnet"


# ---------------------------------------------------------------------------
# default_config: integration with resolve_model
# ---------------------------------------------------------------------------


def test_default_config_cursor_platform() -> None:
    """default_config(platform='cursor') should use cursor-specific model resolution."""
    cfg = default_config(platform="cursor")
    # Architect roles get opus on cursor
    assert cfg.agents["architect"].model == "opus"
    assert cfg.agents["architect_b"].model == "opus"
    # Reviewer/judge roles get sonnet on cursor
    assert cfg.agents["reviewer"].model == "sonnet"
    assert cfg.agents["judge"].model == "sonnet"
    # Developer/explorer get auto on cursor
    assert cfg.agents["developer"].model == "auto"
    assert cfg.agents["explorer"].model == "auto"
