"""v0.17.0 S1: StubAdapter role-aware fallback for ``critic_drift_verifier``.

The drift-verifier emits ``VERDICT: APPROVED|REJECTED`` lines parsed by
:func:`orchestrator.drift_verifier.parse_drift_verdict`. Without a
role-specific stub, the legacy fallback returned ``[stub:{role}] default-ok``
which the parser cannot interpret as a verdict — every drift-verifier test
that didn't explicitly stub the role broke when v0.16.0 wired the gate.

This module pins the role-aware default so v0.17.0 can flip
``drift_verifier_enabled = True`` without any test regressions.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from adapters.types import AgentInvocation
from stub_adapter import StubAdapter, ok


def _invoke(adapter: StubAdapter, role: str) -> str:
    """Run ``adapter.execute`` synchronously and return ``text``."""
    inv = AgentInvocation(
        role=role,
        prompt="x",
        cwd=Path("/tmp"),
    )
    result = asyncio.run(adapter.execute(inv))
    return result.text


def test_drift_verifier_default_returns_approved_verdict() -> None:
    adapter = StubAdapter({})
    text = _invoke(adapter, "critic_drift_verifier")
    assert "VERDICT: APPROVED" in text


def test_drift_verifier_default_success_true() -> None:
    adapter = StubAdapter({})
    inv = AgentInvocation(
        role="critic_drift_verifier",
        prompt="x",
        cwd=Path("/tmp"),
    )
    result = asyncio.run(adapter.execute(inv))
    assert result.success is True


def test_other_roles_preserve_legacy_fallback() -> None:
    """Non-drift roles still get the canonical ``[stub:{role}] default-ok``."""
    adapter = StubAdapter({})
    text = _invoke(adapter, "architect")
    assert text == "[stub:architect] default-ok"


def test_drift_verifier_explicit_handler_wins() -> None:
    """An explicit stub overrides the role-aware default."""
    adapter = StubAdapter({"critic_drift_verifier": ok("VERDICT: REJECTED\n")})
    text = _invoke(adapter, "critic_drift_verifier")
    assert "VERDICT: REJECTED" in text
