"""WS3 (stabilization-v1): formal tool-scoping capability flag.

Before this change the ONLY signal that the Cursor adapter could not
enforce ``allowed_tools`` was a per-invocation warning emitted *inside*
``CursorAdapter._build_command`` (v0.42.1 F2b). A caller that needs
tool-scoping for correctness (the text-only tournament roles
``critic_t`` / ``synthesizer``, which must run with zero tools) had no
machine-checkable way to discover the gap up-front and route around it —
a silent capability gap: text-only roles ran with FULL tools on Cursor.

This module pins down the formal contract:

* ``CursorAdapter`` declares ``capabilities.supports_tool_scoping is False``.
* ``ClaudeCodeAdapter`` declares ``capabilities.supports_tool_scoping is True``
  (its ``--allowed-tools`` flag enforces the allow-list for real).
* A caller requiring scoping (:func:`adapters.base.require_tool_scoping`)
  *detects* the gap and degrades (returns ``False`` + emits a typed
  warning) for Cursor, while passing through for Claude Code.

RED-ON-HEAD: pre-change there is no ``AdapterCapabilities`` /
``supports_tool_scoping`` symbol anywhere — the imports below fail to
resolve and every test errors at collection.

BROKEN-CONTROL: ``test_broken_control_flipping_flag_disables_degrade``
flips the Cursor flag to ``True`` (the WRONG value) and asserts the
caller's scoping check then *fails to degrade* — proving the flag is the
load-bearing input to the degrade decision, not incidental.
"""

from __future__ import annotations

import dataclasses

import pytest

from adapters.base import (
    AdapterCapabilities,
    PlatformAdapter,
    require_tool_scoping,
)
from adapters.claude_code import ClaudeCodeAdapter
from adapters.cursor import CursorAdapter


# ---------------------------------------------------------------------------
# The capability flag is present and reports the right value per adapter.
# ---------------------------------------------------------------------------


def test_cursor_reports_no_tool_scoping() -> None:
    """The Cursor adapter formally declares it cannot enforce scoping."""
    adapter = CursorAdapter(binaries=("cursor",))
    assert isinstance(adapter.capabilities, AdapterCapabilities)
    assert adapter.capabilities.supports_tool_scoping is False


def test_claude_code_reports_tool_scoping() -> None:
    """The Claude Code adapter enforces ``--allowed-tools`` → flag is True."""
    adapter = ClaudeCodeAdapter()
    assert isinstance(adapter.capabilities, AdapterCapabilities)
    assert adapter.capabilities.supports_tool_scoping is True


def test_base_default_is_conservative() -> None:
    """A subclass that forgets to declare capabilities is treated as unable.

    The conservative default (``supports_tool_scoping=False``) means a new
    adapter is never *silently trusted* to scope tools it cannot enforce.
    """
    assert PlatformAdapter.capabilities.supports_tool_scoping is False
    assert AdapterCapabilities().supports_tool_scoping is False


def test_capabilities_is_immutable() -> None:
    """The declared capability set is frozen — callers can't mutate it."""
    caps = AdapterCapabilities(supports_tool_scoping=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        caps.supports_tool_scoping = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# A caller requiring scoping detects the gap and degrades / passes through.
# ---------------------------------------------------------------------------


def test_caller_degrades_on_cursor_with_scoping_intent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Caller requiring scoping on Cursor → returns False AND warns (degrade).

    ``allowed_tools=[]`` is the text-only "no tools" intent. Cursor cannot
    enforce it, so the guard must report ``False`` (caller should degrade)
    and emit the typed ``adapter.tool_scoping_unenforceable`` warning so
    the gap is observable instead of silently granting full tools.
    """
    adapter = CursorAdapter(binaries=("cursor",))
    enforceable = require_tool_scoping(
        adapter, role="critic_t", allowed_tools=[]
    )
    assert enforceable is False
    combined = "".join(capsys.readouterr())
    assert "adapter.tool_scoping_unenforceable" in combined
    assert "warning" in combined.lower()


def test_caller_passes_through_on_claude_with_scoping_intent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Caller requiring scoping on Claude Code → returns True, no warning."""
    adapter = ClaudeCodeAdapter()
    enforceable = require_tool_scoping(
        adapter, role="critic_t", allowed_tools=[]
    )
    assert enforceable is True
    combined = "".join(capsys.readouterr())
    assert "adapter.tool_scoping_unenforceable" not in combined


def test_no_scoping_intent_is_vacuously_satisfied(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``allowed_tools=None`` → nothing to enforce → True, no warning.

    Even on Cursor (which cannot scope), a caller that requested no
    scoping has nothing to degrade — the guard must not cry wolf.
    """
    adapter = CursorAdapter(binaries=("cursor",))
    enforceable = require_tool_scoping(
        adapter, role="developer", allowed_tools=None
    )
    assert enforceable is True
    combined = "".join(capsys.readouterr())
    assert "adapter.tool_scoping_unenforceable" not in combined


# ---------------------------------------------------------------------------
# BROKEN-CONTROL: the flag is the load-bearing input to the degrade path.
# ---------------------------------------------------------------------------


def test_broken_control_flipping_flag_disables_degrade(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Flip Cursor's flag to True (WRONG) → caller no longer degrades.

    This is the broken-control: if ``supports_tool_scoping`` were
    incorrectly ``True`` for Cursor, ``require_tool_scoping`` would
    wrongly report the (unenforceable) scoping as enforceable and emit no
    warning — exactly the silent capability gap WS3 closes. Asserting the
    degrade DISAPPEARS when the flag flips proves the flag drives the
    decision (not some incidental code path).
    """
    adapter = CursorAdapter(binaries=("cursor",))
    # Sanity: with the correct flag the caller degrades + warns.
    assert require_tool_scoping(adapter, role="critic_t", allowed_tools=[]) is False
    capsys.readouterr()  # drain the warning from the sanity call

    # Now wrongly assert the capability. Use the instance to avoid mutating
    # the class-level default for other tests.
    adapter.capabilities = AdapterCapabilities(supports_tool_scoping=True)
    enforceable = require_tool_scoping(
        adapter, role="critic_t", allowed_tools=[]
    )
    assert enforceable is True  # degrade path is now (wrongly) skipped
    combined = "".join(capsys.readouterr())
    assert "adapter.tool_scoping_unenforceable" not in combined


def test_cursor_class_default_unchanged_after_instance_override() -> None:
    """The broken-control instance override must not leak to the class.

    Guards against test cross-contamination: the class-level Cursor
    capability stays ``False`` even after an instance flips its own copy.
    """
    assert CursorAdapter.capabilities.supports_tool_scoping is False


@pytest.mark.parametrize(
    "factory,role,tools,expect_enforceable,expect_warn",
    [
        (lambda: CursorAdapter(binaries=("cursor",)), "critic_t", [], False, True),
        (lambda: CursorAdapter(binaries=("cursor",)), "synthesizer", ["Read"], False, True),
        (lambda: CursorAdapter(binaries=("cursor",)), "developer", None, True, False),
        (ClaudeCodeAdapter, "critic_t", [], True, False),
        (ClaudeCodeAdapter, "developer", ["Edit"], True, False),
    ],
)
def test_require_tool_scoping_matrix(
    factory: object,
    role: str,
    tools: list[str] | None,
    expect_enforceable: bool,
    expect_warn: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Capability-driven degrade/pass matrix across both adapters."""
    adapter = factory()  # type: ignore[operator]
    enforceable = require_tool_scoping(adapter, role=role, allowed_tools=tools)
    assert enforceable is expect_enforceable
    combined = "".join(capsys.readouterr())
    warned = "adapter.tool_scoping_unenforceable" in combined
    assert warned is expect_warn
