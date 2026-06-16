"""Tests for v0.28.0 Bug 2: tournament classifier auth-class subtypes.

The tournament retry loop (``AdapterLLMClient`` in :mod:`tournament.llm`)
must distinguish three new failure-subtype classes that the adapter now
surfaces from ``api_error_status``:

  * ``"auth_failed"`` — 401/403 from the upstream API. Deterministic
    (the same prompt cannot succeed without operator action), so the
    classifier short-circuits without retry AND raises the typed
    :class:`tournament.errors.AuthenticationFailedError` so the
    orchestrator can catch it at the top level and abort the phase
    loop cleanly.
  * ``"rate_limited"`` — 429. Transient (a backoff window will clear
    it), so the classifier returns :class:`TransientError` and the
    tenacity retry loop runs.
  * ``"client_error"`` — 4xx other than 401/403/429. Deterministic
    like a 400 (bad-request style) — short-circuit without retry,
    but as a generic :class:`TournamentError` (the orchestrator does
    NOT abort the run on a 4xx that isn't auth).

Plus a regression: the legacy substring classifier must NOT
accidentally hit ``_TRANSIENT_SUBSTRINGS`` for a plain "403" string
(no substring in the transient list contains "403").
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from errors import TournamentError
from tournament.errors import AuthenticationFailedError
from tournament.llm import (
    AdapterLLMClient,
)
from tournament.llm import _classify_error  # type: ignore


# ── StubAdapter (fakes Phase-2's PlatformAdapter) ─────────────────────────


class _Result:
    def __init__(
        self,
        success: bool = True,
        text: str = "OK",
        error: str | None = None,
        subtype: str | None = None,
    ) -> None:
        self.success = success
        self.text = text
        self.error = error
        self.duration_s = 0.01
        self.subtype = subtype


class StubAdapter:
    """Deterministic PlatformAdapter surrogate recording invocations."""

    def __init__(self, responses: list[_Result | BaseException]) -> None:
        self._responses = list(responses)
        self.calls: list[Any] = []

    async def execute(self, inv: Any) -> _Result:
        self.calls.append(inv)
        if not self._responses:
            raise AssertionError("StubAdapter ran out of scripted responses")
        r = self._responses.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r


def _patch_no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch tenacity's sleep to make retry tests run instantly."""
    import tenacity

    async def _no_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr("asyncio.sleep", _no_sleep)

    class _ZeroWait:
        def __call__(self, _retry_state: Any) -> float:
            return 0.0

    monkeypatch.setattr(tenacity, "wait_exponential", lambda **kw: _ZeroWait())


# ── 1: auth_failed → AuthenticationFailedError, no retry ──────────────────


@pytest.mark.asyncio
async def test_auth_failed_subtype_raises_authentication_failed_error_no_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``subtype="auth_failed"`` raises typed :class:`AuthenticationFailedError`
    immediately — no retry attempts beyond the first call."""
    _patch_no_sleep(monkeypatch)

    adapter = StubAdapter(
        [
            _Result(
                success=False,
                text="",
                error="Failed to authenticate. API Error: 403",
                subtype="auth_failed",
            ),
        ]
    )
    client = AdapterLLMClient(adapter, cwd=tmp_path, max_attempts=5)
    with pytest.raises(AuthenticationFailedError):
        await client.call(system="s", user="u", role="critic_t")
    # No retries — exactly one adapter call.
    assert len(adapter.calls) == 1
    # And it must be a TournamentError too (subclass).
    assert issubclass(AuthenticationFailedError, TournamentError)


# ── 2: rate_limited → TransientError, retries ─────────────────────────────


@pytest.mark.asyncio
async def test_rate_limited_subtype_classified_as_transient_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``subtype="rate_limited"`` is transient — first call raises,
    second call recovers."""
    _patch_no_sleep(monkeypatch)

    adapter = StubAdapter(
        [
            _Result(
                success=False,
                text="",
                error="rate limit window not yet expired",
                subtype="rate_limited",
            ),
            _Result(text="RECOVERED", subtype="success"),
        ]
    )
    client = AdapterLLMClient(adapter, cwd=tmp_path, max_attempts=5)
    out = await client.call(system="s", user="u", role="critic_t")
    assert out == "RECOVERED"
    assert len(adapter.calls) == 2


# ── 3: client_error (4xx other than 429) → no retry ───────────────────────


@pytest.mark.asyncio
async def test_client_error_subtype_does_not_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``subtype="client_error"`` (e.g. a 400) is deterministic — fail
    fast as :class:`TournamentError`, not auth, and do not retry."""
    _patch_no_sleep(monkeypatch)

    adapter = StubAdapter(
        [
            _Result(
                success=False,
                text="",
                error="bad request body",
                subtype="client_error",
            ),
        ]
    )
    client = AdapterLLMClient(adapter, cwd=tmp_path, max_attempts=5)
    with pytest.raises(TournamentError) as excinfo:
        await client.call(system="s", user="u", role="critic_t")
    # NOT the auth-typed subclass — a plain TournamentError so the
    # orchestrator does not abort the loop on a 400.
    assert not isinstance(excinfo.value, AuthenticationFailedError)
    assert len(adapter.calls) == 1


# ── 4: legacy substring regression — "403" alone is NOT transient ────────


def test_legacy_error_string_403_substring_does_not_match_transient() -> None:
    """A bare "403" in the error string must not accidentally classify as
    a transient via :data:`_TRANSIENT_SUBSTRINGS`.

    Pre-Bug-2, no substring in the transient list contained "403"; this
    test pins that contract so a future edit cannot regress it (which
    would silently retry auth failures the way the production stall did).
    """
    # Plain 403 string with no transient substrings → not classified.
    cls = _classify_error("Failed to authenticate. API Error: 403")
    assert cls is None
    # 401 likewise.
    cls = _classify_error("API Error: 401 Unauthorized")
    assert cls is None
    # 400 likewise (bad request).
    cls = _classify_error("API Error: 400 Bad Request")
    assert cls is None
