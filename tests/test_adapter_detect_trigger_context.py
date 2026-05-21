"""v0.37.0 H4: adapter trigger-context routing.

When ``autodev`` is invoked from inside a Claude Code session
(``CLAUDECODE=1`` / ``CLAUDE_PROJECT_DIR``) the ``claude_code`` adapter
must be auto-selected in ``preferred="auto"`` mode; from a Cursor
terminal (``TERM_PROGRAM=Cursor`` / ``CURSOR_*``) the ``cursor`` adapter
must be auto-selected. Explicit ``--platform X`` always wins; the
``respect_trigger_context=False`` escape hatch restores the pre-v0.37.0
env-first precedence.
"""

from __future__ import annotations

import os

import pytest
from unittest.mock import AsyncMock, patch

from adapters.claude_code import ClaudeCodeAdapter
from adapters.cursor import CursorAdapter
from adapters.detect import _detect_trigger_context, detect_platform


@pytest.fixture(autouse=True)
def _clear_trigger_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from a clean env so the developer shell doesn't bleed in."""
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    monkeypatch.delenv("AUTODEV_PLATFORM", raising=False)
    monkeypatch.delenv("AUTODEV_LANG_WEIGHT", raising=False)
    # v0.38.0 HK8: multiplexer envs leak in from the developer's tmux
    # session — clear them so the warning test fires deterministically.
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("STY", raising=False)
    for key in [k for k in list(os.environ) if k.startswith("CURSOR_")]:
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# _detect_trigger_context unit tests
# ---------------------------------------------------------------------------


def test_helper_none_when_no_env() -> None:
    assert _detect_trigger_context() is None


def test_helper_claudecode_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    assert _detect_trigger_context() == "claude_code"


def test_helper_claudecode_zero_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only ``CLAUDECODE=1`` counts — bare presence isn't enough."""
    monkeypatch.setenv("CLAUDECODE", "0")
    assert _detect_trigger_context() is None


def test_helper_claude_project_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/tmp/x")
    assert _detect_trigger_context() == "claude_code"


def test_helper_term_program_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM_PROGRAM", "Cursor")
    assert _detect_trigger_context() == "cursor"


def test_helper_cursor_prefix_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CURSOR_VERSION", "1.0")
    assert _detect_trigger_context() == "cursor"


def test_helper_claude_wins_when_both_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nested-shell edge case: Claude context wins."""
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("TERM_PROGRAM", "Cursor")
    assert _detect_trigger_context() == "claude_code"


# ---------------------------------------------------------------------------
# detect_platform integration with trigger-context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claudecode_env_picks_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    with patch.object(
        ClaudeCodeAdapter, "healthcheck", AsyncMock(return_value=(True, "ok"))
    ):
        name = await detect_platform("auto")
    assert name == "claude_code"


@pytest.mark.asyncio
async def test_claude_project_dir_picks_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/tmp/x")
    with patch.object(
        ClaudeCodeAdapter, "healthcheck", AsyncMock(return_value=(True, "ok"))
    ):
        name = await detect_platform("auto")
    assert name == "claude_code"


@pytest.mark.asyncio
async def test_term_program_cursor_picks_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TERM_PROGRAM", "Cursor")
    with patch.object(
        CursorAdapter, "healthcheck", AsyncMock(return_value=(True, "ok"))
    ):
        name = await detect_platform("auto")
    assert name == "cursor"


@pytest.mark.asyncio
async def test_cursor_prefix_env_picks_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CURSOR_VERSION", "1.0")
    with patch.object(
        CursorAdapter, "healthcheck", AsyncMock(return_value=(True, "ok"))
    ):
        name = await detect_platform("auto")
    assert name == "cursor"


@pytest.mark.asyncio
async def test_no_trigger_env_falls_through_to_autodev_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTODEV_PLATFORM", "cursor")
    with patch.object(
        CursorAdapter, "healthcheck", AsyncMock(return_value=(True, "ok"))
    ):
        name = await detect_platform("auto")
    assert name == "cursor"


@pytest.mark.asyncio
async def test_trigger_context_beats_autodev_platform_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behaviour change in v0.37.0: trigger context overrides AUTODEV_PLATFORM."""
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("AUTODEV_PLATFORM", "cursor")
    with patch.object(
        ClaudeCodeAdapter, "healthcheck", AsyncMock(return_value=(True, "ok"))
    ):
        name = await detect_platform("auto")
    assert name == "claude_code"


@pytest.mark.asyncio
async def test_explicit_preferred_beats_trigger_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator override is supreme — ``--platform cursor`` wins even inside Claude."""
    monkeypatch.setenv("CLAUDECODE", "1")
    with patch.object(
        CursorAdapter, "healthcheck", AsyncMock(return_value=(True, "ok"))
    ):
        name = await detect_platform("cursor")
    assert name == "cursor"


@pytest.mark.asyncio
async def test_respect_trigger_context_false_disables_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Escape hatch: ``respect_trigger_context=False`` restores env precedence."""
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("AUTODEV_PLATFORM", "cursor")
    with patch.object(
        CursorAdapter, "healthcheck", AsyncMock(return_value=(True, "ok"))
    ):
        name = await detect_platform("auto", respect_trigger_context=False)
    assert name == "cursor"


@pytest.mark.asyncio
async def test_trigger_context_unhealthy_falls_through(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When the trigger-chosen adapter healthchecks bad, fall through and warn."""
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("AUTODEV_PLATFORM", "cursor")
    with (
        patch.object(
            ClaudeCodeAdapter,
            "healthcheck",
            AsyncMock(return_value=(False, "claude binary missing")),
        ),
        patch.object(
            CursorAdapter,
            "healthcheck",
            AsyncMock(return_value=(True, "ok")),
        ),
    ):
        name = await detect_platform("auto")
    assert name == "cursor"
    # structlog writes to stdout; the warning must be emitted before the
    # fallthrough so retros can answer "why did this misroute".
    captured = capsys.readouterr()
    assert "trigger_context_unhealthy" in (captured.out + captured.err)
    assert "claude_code" in (captured.out + captured.err)


# ---------------------------------------------------------------------------
# v0.38.0 HK9: CURSOR_* env allowlist (replaces prefix-match)
# ---------------------------------------------------------------------------


def test_hk9_allowlist_cursor_version_triggers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.38.0 HK9: built-in allowlist covers ``CURSOR_VERSION`` —
    pre-existing v0.37.0 behaviour preserved."""
    monkeypatch.setenv("CURSOR_VERSION", "1.0")
    assert _detect_trigger_context() == "cursor"


def test_hk9_allowlist_cursor_agent_triggers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.38.0 HK9: ``CURSOR_AGENT`` is in the built-in allowlist."""
    monkeypatch.setenv("CURSOR_AGENT", "default")
    assert _detect_trigger_context() == "cursor"


def test_hk9_non_allowlist_cursor_var_does_not_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.38.0 HK9 (bug fix): random ``CURSOR_RC_FILE`` is NOT a
    Cursor IDE signal — the v0.37.0 prefix match was over-eager."""
    monkeypatch.setenv("CURSOR_RC_FILE", "/etc/cursorrc")
    assert _detect_trigger_context() is None


def test_hk9_extra_env_extends_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.38.0 HK9: operators on newer Cursor versions extend the
    allowlist via ``cursor_trigger_env_extra`` without waiting for a
    release."""
    monkeypatch.setenv("CURSOR_RC_FILE", "/etc/cursorrc")
    # With the operator override, the previously-rejected var now triggers.
    assert (
        _detect_trigger_context(extra_cursor_env=["CURSOR_RC_FILE"])
        == "cursor"
    )


@pytest.mark.asyncio
async def test_hk9_detect_platform_threads_extra_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.38.0 HK9: ``detect_platform`` honours
    ``cursor_trigger_env_extra``."""
    monkeypatch.setenv("CURSOR_RC_FILE", "/etc/cursorrc")
    with patch.object(
        CursorAdapter, "healthcheck", AsyncMock(return_value=(True, "ok"))
    ):
        name = await detect_platform(
            "auto",
            cursor_trigger_env_extra=["CURSOR_RC_FILE"],
        )
    assert name == "cursor"


# ---------------------------------------------------------------------------
# v0.38.0 HK8: terminal-multiplexer diagnostic warning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hk8_tmux_triggers_multiplexer_warning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """v0.38.0 HK8: tmux + a trigger context → single forensic
    warning. No behaviour change — Claude still wins the selection."""
    monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,12345,0")
    monkeypatch.setenv("CLAUDECODE", "1")
    with patch.object(
        ClaudeCodeAdapter, "healthcheck", AsyncMock(return_value=(True, "ok"))
    ):
        name = await detect_platform("auto")
    assert name == "claude_code"  # behaviour unchanged
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert "tmux_screen_detected" in out, (
        "HK8: multiplexer warning did not fire under TMUX + trigger context"
    )
    assert "TMUX" in out
    assert "claude_code" in out


@pytest.mark.asyncio
async def test_hk8_screen_triggers_multiplexer_warning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """v0.38.0 HK8: GNU screen STY env also fires the warning."""
    monkeypatch.setenv("STY", "12345.pts-0.host")
    monkeypatch.setenv("CURSOR_VERSION", "1.0")
    with patch.object(
        CursorAdapter, "healthcheck", AsyncMock(return_value=(True, "ok"))
    ):
        name = await detect_platform("auto")
    assert name == "cursor"
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert "tmux_screen_detected" in out
    assert "STY" in out


@pytest.mark.asyncio
async def test_hk8_multiplexer_without_trigger_context_silent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """v0.38.0 HK8: tmux alone (no trigger context) → NO warning.
    The warning is specifically about multiplexer + trigger combos
    that may have caused the wrong selection."""
    monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,12345,0")
    with patch.object(
        ClaudeCodeAdapter, "healthcheck", AsyncMock(return_value=(True, "ok"))
    ):
        await detect_platform("auto")
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert "tmux_screen_detected" not in out


@pytest.mark.asyncio
async def test_hk8_tmux_and_sty_both_set_reports_both(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """v0.38.0 HK8: TMUX + STY simultaneously (nested tmux-in-screen
    or vice versa) reports ``BOTH`` so forensics distinguishes the
    case."""
    monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,12345,0")
    monkeypatch.setenv("STY", "12345.pts-0.host")
    monkeypatch.setenv("CLAUDECODE", "1")
    with patch.object(
        ClaudeCodeAdapter, "healthcheck", AsyncMock(return_value=(True, "ok"))
    ):
        await detect_platform("auto")
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert "tmux_screen_detected" in out
    assert "BOTH" in out
