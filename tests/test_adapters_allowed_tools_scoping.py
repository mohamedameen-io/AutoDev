"""v0.42.1 F2b: make text-only tool-scoping real (None vs []).

Regression guard for the Run-5 no-op: ``allowed_tools=[]`` (the explicit
"no tools" intent used by the text-only tournament roles ``critic_t`` /
``synthesizer``) must NOT be conflated with ``allowed_tools=None`` (no
override). A bare ``if inv.allowed_tools:`` drops ``[]`` and silently grants
ALL tools — exactly the bug this module pins down.

- claude_code: ``None`` omits ``--allowed-tools``; ``["Read","Edit"]`` passes
  ``Read,Edit`` (an auto-approve list). ``[]`` — the text-only "no tools"
  intent — DISABLES tools via the AVAILABILITY flag ``--tools ""``, NOT the
  permission-only no-op ``--allowed-tools ""``. Phase-4 A4 field finding:
  ``--allowed-tools ""`` does NOT remove tools (a Bash-triggering prompt still
  ran Bash under it, ``permission_denials=[]``); ``--tools ""`` does (the model
  has no tool to call). See ``results/phase4/A4-microprobe.json``.
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


def test_claude_empty_list_disables_tools_via_tools_flag() -> None:
    """``allowed_tools=[]`` → text-only "no tools" → ``--tools ""`` (availability).

    Phase-4 A4 field finding (``results/phase4/A4-microprobe.json``):
    ``--allowed-tools ""`` is permission-only and a NO-OP for availability — a
    Bash-triggering prompt still executed Bash under it (``permission_denials``
    empty). The real disable is the ``--tools`` flag: ``--tools ""`` removes all
    built-in tools so the model has nothing to call (verified: 0 ``tool_use``).
    So ``[]`` MUST render ``--tools ""`` and MUST NOT render the no-op
    ``--allowed-tools ""`` (which would leave critic_t/synthesizer with all
    tools and break the S1 bounded-critic guarantee).
    """
    adapter = ClaudeCodeAdapter()
    cmd = adapter._build_command(_make_inv(allowed_tools=[]))
    assert "--tools" in cmd
    idx = cmd.index("--tools")
    assert cmd[idx + 1] == ""
    # The permission-only no-op must NOT be emitted for the empty (no-tools) case.
    assert "--allowed-tools" not in cmd


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
