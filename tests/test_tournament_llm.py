"""Tests for AdapterLLMClient retry behaviour and Phase-2 duck-typing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from errors import TournamentError
from tournament.llm import (
    AdapterLLMClient,
    StubLLMClient,
    TransientError,
)
from tournament.llm import _build_invocation  # type: ignore


# ── StubAdapter (fakes Phase-2's PlatformAdapter) ─────────────────────────

class _Result:
    def __init__(
        self,
        success: bool = True,
        text: str = "OK",
        error: str | None = None,
    ) -> None:
        self.success = success
        self.text = text
        self.error = error
        self.duration_s = 0.01


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


# ── AgentInvocation construction ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_invocation_uses_phase2_type_when_available() -> None:
    """_build_invocation should return an AgentInvocation pydantic model
    when src.adapters.types is importable."""
    inv = _build_invocation(
        role="critic_t",
        system="SYS",
        user="USER",
        cwd=Path("/tmp"),
        model=None,
        timeout_s=600,
    )
    # Phase 2 scaffolding exists for types.py; we should get the real class.
    assert hasattr(inv, "role")
    assert hasattr(inv, "prompt")
    assert inv.role == "critic_t"
    assert inv.prompt == "SYS\n\nUSER"
    assert inv.timeout_s == 600
    assert inv.max_turns == 1
    # Text-only roles get no allowed_tools.
    assert inv.allowed_tools == []


# ── Happy path ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_call_returns_result_text(tmp_path: Path) -> None:
    adapter = StubAdapter([_Result(text="HELLO")])
    client = AdapterLLMClient(adapter, cwd=tmp_path)
    out = await client.call(system="s", user="u", role="critic_t")
    assert out == "HELLO"
    assert len(adapter.calls) == 1
    assert adapter.calls[0].role == "critic_t"


# ── Transient retry ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retries_on_transient_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First call raises TransientError; second succeeds → result returned."""
    # Speed up the test by patching tenacity's sleeper.
    _patch_no_sleep(monkeypatch)

    adapter = StubAdapter(
        [
            TransientError("429 rate limit"),
            _Result(text="RECOVERED"),
        ]
    )
    client = AdapterLLMClient(adapter, cwd=tmp_path, max_attempts=5)
    out = await client.call(system="s", user="u", role="architect_b")
    assert out == "RECOVERED"
    assert len(adapter.calls) == 2


@pytest.mark.asyncio
async def test_retries_on_rate_limit_string_in_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain Exception containing '429' in its message is reclassified transient."""
    _patch_no_sleep(monkeypatch)

    adapter = StubAdapter(
        [
            RuntimeError("Server returned 429 — too many requests"),
            _Result(text="OK"),
        ]
    )
    client = AdapterLLMClient(adapter, cwd=tmp_path, max_attempts=3)
    out = await client.call(system="s", user="u", role="judge")
    assert out == "OK"


@pytest.mark.asyncio
async def test_retries_on_transient_result_success_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """adapter returns success=False with 'overloaded' error → retries."""
    _patch_no_sleep(monkeypatch)

    adapter = StubAdapter(
        [
            _Result(success=False, text="", error="model overloaded (529)"),
            _Result(text="FINALLY"),
        ]
    )
    client = AdapterLLMClient(adapter, cwd=tmp_path, max_attempts=3)
    out = await client.call(system="s", user="u", role="critic_t")
    assert out == "FINALLY"


@pytest.mark.asyncio
async def test_exhausts_retries_raises_transient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_no_sleep(monkeypatch)

    adapter = StubAdapter([TransientError("rate") for _ in range(5)])
    client = AdapterLLMClient(adapter, cwd=tmp_path, max_attempts=3)
    with pytest.raises(TransientError):
        await client.call(system="s", user="u", role="critic_t")
    # 3 attempts total.
    assert len(adapter.calls) == 3


# ── Non-transient errors do NOT retry ─────────────────────────────────────

@pytest.mark.asyncio
async def test_non_transient_does_not_retry(tmp_path: Path) -> None:
    """A permission error is wrapped in TournamentError — no retries."""
    adapter = StubAdapter([RuntimeError("permission denied: not logged in")])
    client = AdapterLLMClient(adapter, cwd=tmp_path, max_attempts=5)

    with pytest.raises(TournamentError):
        await client.call(system="s", user="u", role="critic_t")
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_success_false_non_transient_raises_tournament_error(
    tmp_path: Path,
) -> None:
    """success=False with a non-transient message is NOT retried."""
    adapter = StubAdapter(
        [_Result(success=False, text="", error="invalid prompt schema")]
    )
    client = AdapterLLMClient(adapter, cwd=tmp_path, max_attempts=5)

    with pytest.raises(TournamentError):
        await client.call(system="s", user="u", role="critic_t")
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_missing_text_raises_tournament_error(tmp_path: Path) -> None:
    class NoText:
        success = True
        error = None

    class BadAdapter:
        async def execute(self, inv: Any) -> Any:
            return NoText()

    client = AdapterLLMClient(BadAdapter(), cwd=tmp_path)
    with pytest.raises(TournamentError):
        await client.call(system="s", user="u", role="critic_t")


# ── Timeout boundary (represented as adapter error) ───────────────────────

@pytest.mark.asyncio
async def test_timeout_error_is_transient_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 'timeout' error from the adapter is treated as transient."""
    _patch_no_sleep(monkeypatch)

    adapter = StubAdapter(
        [
            _Result(success=False, text="", error="timed out after 600s"),
            _Result(text="OK"),
        ]
    )
    client = AdapterLLMClient(adapter, cwd=tmp_path, max_attempts=3)
    out = await client.call(system="s", user="u", role="judge")
    assert out == "OK"


# ── StubLLMClient mode tests ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stub_llm_client_records_calls() -> None:
    """StubLLMClient records every call for assertions."""
    client = StubLLMClient(responses={"critic_t": "C", "judge": "RANKING: 1, 2, 3"})
    await client.call(system="s", user="u", role="critic_t")
    await client.call(system="s", user="u", role="judge")
    assert len(client.calls) == 2
    assert client.calls[0]["role"] == "critic_t"
    assert client.calls[1]["role"] == "judge"


@pytest.mark.asyncio
async def test_stub_llm_client_role_nth_response() -> None:
    """Keys of form (role, n) provide per-call responses."""
    client = StubLLMClient(
        responses={
            ("judge", 1): "first",
            ("judge", 2): "second",
            "judge": "default",
        }
    )
    assert await client.call(system="", user="", role="judge") == "first"
    assert await client.call(system="", user="", role="judge") == "second"
    assert await client.call(system="", user="", role="judge") == "default"


@pytest.mark.asyncio
async def test_stub_llm_client_callback_mode() -> None:
    """Callback mode gets (role, system, user)."""
    seen: list[tuple[str, str, str]] = []

    def _fn(role: str, system: str, user: str) -> str:
        seen.append((role, system, user))
        return f"reply-to-{role}"

    client = StubLLMClient(fn=_fn)
    out = await client.call(system="SYS", user="USER", role="architect_b")
    assert out == "reply-to-architect_b"
    assert seen == [("architect_b", "SYS", "USER")]


def test_stub_llm_client_requires_fn_or_responses() -> None:
    with pytest.raises(ValueError):
        StubLLMClient()  # type: ignore[call-arg]


# ── Helpers ───────────────────────────────────────────────────────────────

def _patch_no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch tenacity's sleep to make retry tests run instantly."""
    import tenacity

    async def _no_sleep(_s: float) -> None:
        return None

    # tenacity uses `nap.AsyncioSleep` or similar — the simpler approach is to
    # monkey-patch asyncio.sleep in the tournament.llm module.
    import tournament.llm as llm_mod  # noqa: F401

    monkeypatch.setattr("asyncio.sleep", _no_sleep)

    # Also monkey-patch tenacity's wait by forcing zero wait time.
    class _ZeroWait:
        def __call__(self, _retry_state: Any) -> float:
            return 0.0

    # Apply at module level (tenacity consults wait() per attempt).
    monkeypatch.setattr(tenacity, "wait_exponential", lambda **kw: _ZeroWait())
