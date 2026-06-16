"""v0.42.1 F2b: make text-only tool-scoping real (None vs []).

Regression guard for the Run-5 no-op: ``allowed_tools=[]`` (the explicit
"no tools" intent used by the text-only tournament roles ``critic_t`` /
``synthesizer``) must NOT be conflated with ``allowed_tools=None`` (no
override). A bare ``if inv.allowed_tools:`` drops ``[]`` and silently grants
ALL tools — exactly the bug this module pins down.

- claude_code: ``None`` omits ``--allowed-tools``; ``["Read","Edit"]`` passes
  ``Read,Edit``; ``[]`` passes the flag with an empty allow-list (``""``).
- cursor: there is NO ``--allowed-tools`` mechanism; the adapter only warns.
  ``[]`` (no-tools intent) must surface the same warning as a non-empty list
  instead of being silently swallowed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from adapters.claude_code import ClaudeCodeAdapter
from adapters.cursor import CursorAdapter
from adapters.types import AgentInvocation


def _make_inv(**overrides: object) -> AgentInvocation:
    """Mirror tests/test_adapters_claude_code.py::_make_inv."""
    base: dict[str, object] = dict(
        role="developer",
        prompt="do the thing",
        cwd=Path("/repo"),
        model="opus",
        allowed_tools=["Edit", "Bash"],
        max_turns=3,
    )
    base.update(overrides)
    return AgentInvocation(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# claude_code: None vs [] vs non-empty.
# ---------------------------------------------------------------------------


def test_claude_none_omits_allowed_tools_flag() -> None:
    """``allowed_tools=None`` → no override → flag absent (CLI default = all)."""
    adapter = ClaudeCodeAdapter()
    cmd = adapter._build_command(_make_inv(allowed_tools=None))
    assert "--allowed-tools" not in cmd


def test_claude_non_empty_passes_comma_joined() -> None:
    """A populated list → flag immediately followed by the comma-joined value."""
    adapter = ClaudeCodeAdapter()
    cmd = adapter._build_command(_make_inv(allowed_tools=["Read", "Edit"]))
    assert "--allowed-tools" in cmd
    idx = cmd.index("--allowed-tools")
    assert cmd[idx + 1] == "Read,Edit"


def test_claude_empty_list_passes_empty_allow_list() -> None:
    """``allowed_tools=[]`` → explicit no-tools → flag present with ``""``.

    This is the case that FAILS pre-fix: today ``if inv.allowed_tools:`` is
    falsy for ``[]`` so the flag is omitted and Claude gets ALL tools.
    """
    adapter = ClaudeCodeAdapter()
    cmd = adapter._build_command(_make_inv(allowed_tools=[]))
    assert "--allowed-tools" in cmd
    idx = cmd.index("--allowed-tools")
    assert cmd[idx + 1] == ""


# ---------------------------------------------------------------------------
# cursor: no enforcement mechanism, but [] must still surface the warning.
# ---------------------------------------------------------------------------


def test_cursor_empty_list_emits_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``allowed_tools=[]`` (no-tools intent) → warning is emitted, not dropped.

    Pre-fix the cursor guard is ``if inv.allowed_tools:`` so ``[]`` is
    swallowed silently. Structlog uses PrintLoggerFactory → stdout, so we
    assert on captured output. ``_build_command`` is the synchronous site of
    the warning and adds NO tool-restriction flag (cursor cannot enforce one).
    """
    adapter = CursorAdapter(binaries=("cursor",))
    inv = AgentInvocation(
        role="critic_t",
        prompt="p",
        cwd=tmp_path,
        allowed_tools=[],
    )
    cmd = adapter._build_command("cursor", inv)
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "cursor.allowed_tools_ignored" in combined
    assert "warning" in combined.lower()
    # Cursor has no tool-restriction flag — it must not invent one.
    assert "--allowed-tools" not in cmd


def test_cursor_none_emits_no_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``allowed_tools=None`` (no override) → no warning; [] is distinct."""
    adapter = CursorAdapter(binaries=("cursor",))
    inv = AgentInvocation(
        role="developer",
        prompt="p",
        cwd=tmp_path,
        allowed_tools=None,
    )
    cmd = adapter._build_command("cursor", inv)
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "cursor.allowed_tools_ignored" not in combined
    assert "--allowed-tools" not in cmd
