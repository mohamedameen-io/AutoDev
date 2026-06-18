"""Engagement proof for StubAdapter STRICT mode (B1 / gate N3).

The default StubAdapter fabricates permissive gate-PASS values for several
safety-critical roles when a test forgets to stub them. That silently masks the
rejection / failure path the test believes it is exercising. ``STUB_STRICT=1``
turns those silent passes into a loud ``StubMissingError``.

Non-vacuity: the first test PROVES the masked permissive path exists (without
STUB_STRICT, an unstubbed safety-critical role yields a fabricated success).
Were that path removed, the strict-raises test would no longer be guarding a
real failure mode.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.stub_adapter import (
    SAFETY_CRITICAL_ROLES,
    StubAdapter,
    StubMissingError,
    ok,
)

from adapters.types import AgentInvocation


def _inv(role: str, prompt: str = "") -> AgentInvocation:
    return AgentInvocation(role=role, prompt=prompt, cwd=Path("/tmp"))


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def test_without_strict_unstubbed_critical_role_returns_permissive_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NON-VACUITY: the masked permissive path exists when STUB_STRICT is unset.

    An unstubbed ``critic_drift_verifier`` fabricates ``VERDICT: APPROVED`` —
    a gate-PASS the test never asked for. This is exactly what strict mode
    must refuse to do.
    """
    monkeypatch.delenv("STUB_STRICT", raising=False)
    adapter = StubAdapter(responses={})
    result = _run(adapter.execute(_inv("critic_drift_verifier")))
    assert result.success is True
    assert "APPROVED" in result.text


def test_strict_raises_for_unstubbed_safety_critical_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WITH STUB_STRICT=1 an unstubbed safety-critical role raises."""
    monkeypatch.setenv("STUB_STRICT", "1")
    adapter = StubAdapter(responses={})
    with pytest.raises(StubMissingError, match="critic_drift_verifier"):
        _run(adapter.execute(_inv("critic_drift_verifier")))


@pytest.mark.parametrize("role", sorted(SAFETY_CRITICAL_ROLES))
def test_strict_raises_for_every_safety_critical_role(
    role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every declared safety-critical role is guarded under STUB_STRICT=1."""
    monkeypatch.setenv("STUB_STRICT", "1")
    adapter = StubAdapter(responses={})
    with pytest.raises(StubMissingError, match=role):
        _run(adapter.execute(_inv(role)))


def test_strict_honors_explicit_stub_for_critical_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WITH STUB_STRICT=1 but the role explicitly stubbed: no raise."""
    monkeypatch.setenv("STUB_STRICT", "1")
    adapter = StubAdapter(
        responses={"critic_drift_verifier": ok("VERDICT: REJECTED\n")}
    )
    result = _run(adapter.execute(_inv("critic_drift_verifier")))
    assert result.success is True
    assert "REJECTED" in result.text


def test_strict_does_not_guard_non_critical_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Strict only guards safety-critical roles.

    A non-critical role (``developer``) under STUB_STRICT=1 with no stub still
    returns the generic default — its fallback is inert text, not a fabricated
    gate verdict, so there is nothing to mask.
    """
    assert "developer" not in SAFETY_CRITICAL_ROLES
    monkeypatch.setenv("STUB_STRICT", "1")
    adapter = StubAdapter(responses={})
    result = _run(adapter.execute(_inv("developer")))
    assert result.success is True
    assert result.text == "[stub:developer] default-ok"


def test_unset_strict_leaves_all_critical_defaults_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default (env unset): every safety-critical role still returns a default.

    Guards the "behavior UNCHANGED when STUB_STRICT is unset" contract for the
    whole role set, not just the drift verifier.
    """
    monkeypatch.delenv("STUB_STRICT", raising=False)
    adapter = StubAdapter(responses={})
    for role in SAFETY_CRITICAL_ROLES:
        result = _run(adapter.execute(_inv(role)))
        assert result.success is True, role
