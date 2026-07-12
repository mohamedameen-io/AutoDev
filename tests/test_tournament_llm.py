"""Tests for AdapterLLMClient retry behaviour and Phase-2 duck-typing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from errors import TournamentError
from tournament.llm import (
    _TEXT_ONLY_NO_TOOL_ROLES,
    AdapterLLMClient,
    ExpensiveTransientError,
    StubLLMClient,
    TransientError,
)
from tournament.llm import _build_invocation, _classify_error  # type: ignore


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


# ── AgentInvocation construction ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_invocation_uses_phase2_type_when_available() -> None:
    """_build_invocation should return an AgentInvocation pydantic model
    when src.adapters.types is importable.

    With no per-role overrides, the defaults are ``max_turns=1`` and
    ``allowed_tools=None`` (i.e. flag omitted upstream — preserves original
    "all tools available" behavior for callers that don't opt in).
    """
    inv = _build_invocation(
        role="critic_t",
        system="SYS",
        user="USER",
        cwd=Path("/tmp"),
        model=None,
        timeout_s=600,
    )
    assert hasattr(inv, "role")
    assert hasattr(inv, "prompt")
    assert inv.role == "critic_t"
    assert inv.prompt == "SYS\n\nUSER"
    assert inv.timeout_s == 600
    assert inv.max_turns == 1
    # Default: no tool restriction → flag omitted by adapter.
    assert inv.allowed_tools is None
    # Default: no effort hint → adapter inherits user-global default.
    assert inv.effort is None


# ── Fix 2 + Fix 3: per-role max_turns and allowed_tools ───────────────────


@pytest.mark.asyncio
async def test_invocation_uses_role_max_turns(tmp_path: Path) -> None:
    """``role_max_turns`` mapping is honored per-call."""
    adapter = StubAdapter([_Result(text="A"), _Result(text="B")])
    client = AdapterLLMClient(
        adapter,
        cwd=tmp_path,
        role_max_turns={"architect_b": 5, "critic_t": 1},
    )
    await client.call(system="s", user="u", role="architect_b")
    await client.call(system="s", user="u", role="critic_t")

    assert adapter.calls[0].role == "architect_b"
    assert adapter.calls[0].max_turns == 5
    assert adapter.calls[1].role == "critic_t"
    assert adapter.calls[1].max_turns == 1


@pytest.mark.asyncio
async def test_invocation_uses_role_allowed_tools_empty_list(
    tmp_path: Path,
) -> None:
    """An empty list in the role map is normalized to ``["Read"]``.

    The Claude CLI's ``--allowed-tools`` flag is variadic and is omitted by
    the adapter when ``allowed_tools`` is falsy — which would silently leave
    all tools available. We pick a single read-only sentinel instead.
    """
    adapter = StubAdapter([_Result(text="A")])
    client = AdapterLLMClient(
        adapter,
        cwd=tmp_path,
        role_allowed_tools={"architect_b": []},
    )
    await client.call(system="s", user="u", role="architect_b")
    assert adapter.calls[0].allowed_tools == ["Read"]


@pytest.mark.asyncio
async def test_invocation_uses_role_allowed_tools_nonempty(
    tmp_path: Path,
) -> None:
    """A non-empty list passes through unchanged."""
    adapter = StubAdapter([_Result(text="A")])
    client = AdapterLLMClient(
        adapter,
        cwd=tmp_path,
        role_allowed_tools={"architect_b": ["Read", "Grep"]},
    )
    await client.call(system="s", user="u", role="architect_b")
    assert adapter.calls[0].allowed_tools == ["Read", "Grep"]


@pytest.mark.asyncio
async def test_invocation_unknown_role_uses_defaults(tmp_path: Path) -> None:
    """A role not in either map falls back to ``max_turns=1`` and no tools."""
    adapter = StubAdapter([_Result(text="A")])
    client = AdapterLLMClient(
        adapter,
        cwd=tmp_path,
        role_max_turns={"architect_b": 5},
        role_allowed_tools={"architect_b": ["Read"]},
    )
    await client.call(system="s", user="u", role="judge")
    assert adapter.calls[0].max_turns == 1
    assert adapter.calls[0].allowed_tools is None


@pytest.mark.asyncio
async def test_invocation_no_role_dicts_keeps_defaults(tmp_path: Path) -> None:
    """Without role dicts, ``AdapterLLMClient`` behaves exactly as before
    Fix 2/3 — preserving back-compat with existing callers."""
    adapter = StubAdapter([_Result(text="A")])
    client = AdapterLLMClient(adapter, cwd=tmp_path)
    await client.call(system="s", user="u", role="critic_t")
    assert adapter.calls[0].max_turns == 1
    assert adapter.calls[0].allowed_tools is None
    assert adapter.calls[0].effort is None


# ── v0.41.0 A4: text-only roles drop Read (no error_max_turns) ────────────


def test_text_only_no_tool_roles_set_membership() -> None:
    """The text-only-no-tool set is exactly ``{critic_t, synthesizer}``.

    architect_b is deliberately EXCLUDED — WS-5 grants it a non-empty
    Read + Bash registry tool set so the plan critic can execute a reproduction
    and empirically falsify a suspect acceptance oracle. It must therefore NOT
    be forced to an empty tool list.
    """
    assert _TEXT_ONLY_NO_TOOL_ROLES == frozenset({"critic_t", "synthesizer"})
    assert "architect_b" not in _TEXT_ONLY_NO_TOOL_ROLES
    assert "judge" not in _TEXT_ONLY_NO_TOOL_ROLES


@pytest.mark.parametrize("role", ["critic_t", "synthesizer"])
def test_text_only_role_resolves_to_empty_tools_over_empty_sentinel(
    tmp_path: Path, role: str
) -> None:
    """critic_t / synthesizer resolve to an EMPTY tool list even when the
    override map configured ``[]`` — the empty→["Read"] sentinel is NOT
    re-applied for these roles. With Read dropped + inline content the role
    can never burn its only turn on a speculative read.
    """
    adapter = StubAdapter([_Result(text="A")])
    client = AdapterLLMClient(
        adapter,
        cwd=tmp_path,
        # Same shape the phase_review_runner builds for a text-only role.
        role_allowed_tools={role: []},
    )
    # The internal resolver returns [] (not ["Read"]).
    assert client._resolve_allowed_tools(role) == []


@pytest.mark.parametrize("role", ["critic_t", "synthesizer"])
def test_text_only_role_unconfigured_keeps_legacy_none(
    tmp_path: Path, role: str
) -> None:
    """Back-compat: the A4 suppression fires only for CONFIGURED text-only
    roles (the tournament runners always configure them with ``[]``). A
    caller that never opted into per-role tool restriction — no map, or this
    role absent — keeps the legacy ``None`` (adapter omits the flag). This
    prevents the A4 fix from silently restricting un-opted-in callers."""
    # No role map at all → legacy None.
    client_none = AdapterLLMClient(StubAdapter([_Result()]), cwd=tmp_path)
    assert client_none._resolve_allowed_tools(role) is None
    # Map present but THIS role absent → legacy None.
    client_other = AdapterLLMClient(
        StubAdapter([_Result()]),
        cwd=tmp_path,
        role_allowed_tools={"architect_b": []},
    )
    assert client_other._resolve_allowed_tools(role) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["critic_t", "synthesizer"])
async def test_text_only_invocation_has_no_read_tool(
    tmp_path: Path, role: str
) -> None:
    """A critic_t / synthesizer invocation built by the client carries an
    EMPTY ``allowed_tools`` list — Read is never granted. The call completes
    (StubAdapter returns inline-derived text) with no error_max_turns path.
    """
    adapter = StubAdapter([_Result(text="REVIEW-FROM-INLINE-CONTENT")])
    client = AdapterLLMClient(
        adapter,
        cwd=tmp_path,
        role_allowed_tools={role: []},
        role_max_turns={role: 6},
    )
    out = await client.call(system="s", user="inline content here", role=role)
    assert out == "REVIEW-FROM-INLINE-CONTENT"
    assert adapter.calls[0].role == role
    assert adapter.calls[0].allowed_tools == []
    assert adapter.calls[0].allowed_tools != ["Read"]


def test_non_text_only_role_gets_read_sentinel_for_empty_list(
    tmp_path: Path,
) -> None:
    """Regression guard: a non-text-only role configured with ``[]`` keeps the
    legacy empty→["Read"] normalization.

    ``judge`` is the vehicle: it is excluded from the text-only-no-tool set
    (so the empty->[] suppression does NOT apply) yet has empty canonical
    tools, so ``_build_role_overrides`` configures it with ``[]`` and the
    sentinel fires. (Pre-WS-5 this guard used architect_b, but WS-5 grants
    architect_b a non-empty Read + Bash set, so it no longer exercises the
    empty-list sentinel path.)
    """
    client = AdapterLLMClient(
        StubAdapter([_Result()]),
        cwd=tmp_path,
        role_allowed_tools={"judge": []},
    )
    assert client._resolve_allowed_tools("judge") == ["Read"]


# ── Step 3: per-role effort plumbing ──────────────────────────────────────


@pytest.mark.asyncio
async def test_invocation_uses_role_effort(tmp_path: Path) -> None:
    """``role_effort`` mapping is honored per-call (mirrors role_max_turns)."""
    adapter = StubAdapter([_Result(text="A"), _Result(text="B")])
    client = AdapterLLMClient(
        adapter,
        cwd=tmp_path,
        role_effort={"architect_b": "high", "critic_t": "medium"},
    )
    await client.call(system="s", user="u", role="architect_b")
    await client.call(system="s", user="u", role="critic_t")

    assert adapter.calls[0].role == "architect_b"
    assert adapter.calls[0].effort == "high"
    assert adapter.calls[1].role == "critic_t"
    assert adapter.calls[1].effort == "medium"


@pytest.mark.asyncio
async def test_invocation_unknown_role_effort_returns_none(tmp_path: Path) -> None:
    """A role not in ``role_effort`` falls back to ``effort=None``."""
    adapter = StubAdapter([_Result(text="A")])
    client = AdapterLLMClient(
        adapter,
        cwd=tmp_path,
        role_effort={"architect_b": "high"},
    )
    await client.call(system="s", user="u", role="judge")
    assert adapter.calls[0].effort is None


@pytest.mark.asyncio
async def test_invocation_no_role_effort_dict(tmp_path: Path) -> None:
    """``role_effort=None`` (the default) → ``effort=None`` on every call."""
    adapter = StubAdapter([_Result(text="A")])
    client = AdapterLLMClient(adapter, cwd=tmp_path)
    await client.call(system="s", user="u", role="architect_b")
    assert adapter.calls[0].effort is None


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
async def test_retries_on_empty_stderr_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 'empty stderr' sentinel from Tier 1A is treated as transient."""
    _patch_no_sleep(monkeypatch)

    adapter = StubAdapter(
        [
            _Result(
                success=False,
                text="",
                error="claude exited 1 with empty stderr",
            ),
            _Result(text="OK"),
        ]
    )
    client = AdapterLLMClient(adapter, cwd=tmp_path, max_attempts=5)
    out = await client.call(system="s", user="u", role="architect_b")
    assert out == "OK"
    assert len(adapter.calls) == 2


@pytest.mark.asyncio
async def test_retries_on_claude_exited_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any 'claude exited' prefix from the adapter is treated as transient.

    No ``subtype`` is provided (genuine subprocess death without parsable
    JSON output), so this falls through to the substring classifier.
    """
    _patch_no_sleep(monkeypatch)

    adapter = StubAdapter(
        [
            _Result(success=False, text="", error="claude exited 1: some text"),
            _Result(text="OK"),
        ]
    )
    client = AdapterLLMClient(adapter, cwd=tmp_path, max_attempts=5)
    out = await client.call(system="s", user="u", role="architect_b")
    assert out == "OK"
    assert len(adapter.calls) == 2


# ── Fix 4: deterministic-subtype short-circuit (no retry) ─────────────────


@pytest.mark.asyncio
async def test_error_max_turns_does_not_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``subtype="error_max_turns"`` is deterministic — fail fast, no retry."""
    _patch_no_sleep(monkeypatch)

    adapter = StubAdapter(
        [
            _Result(
                success=False,
                text="",
                error="error_max_turns hit",
                subtype="error_max_turns",
            ),
        ]
    )
    client = AdapterLLMClient(adapter, cwd=tmp_path, max_attempts=5)
    with pytest.raises(TournamentError):
        await client.call(system="s", user="u", role="architect_b")
    # No retries — exactly one adapter call.
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_subtype_overrides_transient_substring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if the error string contains 'claude exited', a deterministic
    subtype short-circuits the retry loop."""
    _patch_no_sleep(monkeypatch)

    adapter = StubAdapter(
        [
            _Result(
                success=False,
                text="",
                error="claude exited 1: error_max_turns",
                subtype="error_max_turns",
            ),
        ]
    )
    client = AdapterLLMClient(adapter, cwd=tmp_path, max_attempts=5)
    with pytest.raises(TournamentError):
        await client.call(system="s", user="u", role="architect_b")
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_subtype_success_keeps_happy_path(tmp_path: Path) -> None:
    """``subtype="success"`` on a successful response is a no-op."""
    adapter = StubAdapter([_Result(text="HELLO", subtype="success")])
    client = AdapterLLMClient(adapter, cwd=tmp_path)
    out = await client.call(system="s", user="u", role="critic_t")
    assert out == "HELLO"


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


# ── v0.39.0 B4: jitter on the retry backoff ───────────────────────────────


def _capture_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Capture (but skip) every ``asyncio.sleep`` duration tenacity asks
    for, so we can introspect the computed wait per attempt without
    actually sleeping. Unlike ``_patch_no_sleep`` this does NOT stub out
    ``wait_exponential`` — the real (jittered) wait strategy runs."""
    captured: list[float] = []

    async def _record(s: float) -> None:
        captured.append(s)

    monkeypatch.setattr("asyncio.sleep", _record)
    return captured


@pytest.mark.asyncio
async def test_retry_backoff_is_jittered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The configured ``wait`` is a combined exponential + 0-2s random
    jitter (not a bare exponential). Two independent retry sequences for
    the SAME attempt index must not produce identical backoff durations,
    proving the ``wait_random`` term is live. Attempt-count semantics are
    unchanged (the existing count tests still pass).
    """
    import random

    sleeps_a = _capture_sleeps(monkeypatch)
    random.seed(1)
    adapter_a = StubAdapter([TransientError("429") for _ in range(5)])
    client_a = AdapterLLMClient(adapter_a, cwd=tmp_path, max_attempts=4)
    with pytest.raises(TransientError):
        await client_a.call(system="s", user="u", role="critic_t")

    sleeps_b = _capture_sleeps(monkeypatch)
    random.seed(2)
    adapter_b = StubAdapter([TransientError("429") for _ in range(5)])
    client_b = AdapterLLMClient(adapter_b, cwd=tmp_path, max_attempts=4)
    with pytest.raises(TransientError):
        await client_b.call(system="s", user="u", role="critic_t")

    # Each sequence sleeps between attempts (4 attempts → 3 sleeps).
    assert len(sleeps_a) >= 1
    assert len(sleeps_b) >= 1
    # A bare exponential would give identical, deterministic durations for
    # the same attempt index across the two runs. The 0-2s jitter breaks
    # that determinism: the first inter-attempt sleeps must differ.
    assert sleeps_a[0] != sleeps_b[0]
    # Sanity: jitter rides ON TOP of the exponential floor (min=2), so the
    # first sleep is >= 2.0 and within the exponential+jitter envelope.
    assert sleeps_a[0] >= 2.0
    assert sleeps_a[0] <= 4.0 + 1e-6  # first exp term (~2) + max jitter (2)


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


# ── v0.5.4 Part 1B: Expensive-transient retry cap ─────────────────────────


@pytest.mark.asyncio
async def test_call_caps_expensive_transient_at_3_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timeout error retries at most ``max_attempts_expensive=3`` times.

    Without the cap, a 600s timeout retried 5x would burn ~50 minutes silently.
    """
    _patch_no_sleep(monkeypatch)

    adapter = StubAdapter(
        [
            _Result(success=False, text="", error="timeout after 600s")
            for _ in range(10)
        ]
    )
    client = AdapterLLMClient(
        adapter, cwd=tmp_path, max_attempts=5, max_attempts_expensive=3
    )
    with pytest.raises((ExpensiveTransientError, TransientError)):
        await client.call(system="s", user="u", role="judge")
    assert len(adapter.calls) == 3


@pytest.mark.asyncio
async def test_call_normal_transient_uses_default_5_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rate-limit (normal transient) still retries up to ``max_attempts=5``."""
    _patch_no_sleep(monkeypatch)

    adapter = StubAdapter(
        [
            _Result(success=False, text="", error="rate limit hit (429)")
            for _ in range(10)
        ]
    )
    client = AdapterLLMClient(
        adapter, cwd=tmp_path, max_attempts=5, max_attempts_expensive=3
    )
    with pytest.raises(TransientError):
        await client.call(system="s", user="u", role="judge")
    assert len(adapter.calls) == 5


def test_classify_error_categorises_expensive_vs_normal() -> None:
    """``_classify_error`` returns the correct exception class per substring."""
    # Expensive transient substrings → ExpensiveTransientError
    for msg in (
        "timeout after 600s",
        "request timed out",
        "TIMEOUT",
        "Timed Out",
    ):
        cls = _classify_error(msg)
        assert cls is ExpensiveTransientError, f"{msg!r} should be expensive"

    # Normal transient substrings → TransientError (not ExpensiveTransientError)
    for msg in (
        "429 too many requests",
        "model overloaded (529)",
        "rate limit",
        "503 service unavailable",
        "claude exited 1",
        "empty stderr",
        "broken pipe",
        "connection reset",
    ):
        cls = _classify_error(msg)
        assert cls is TransientError, f"{msg!r} should be normal transient"

    # Non-transient → None
    for msg in (
        "permission denied: not logged in",
        "invalid prompt schema",
        "auth failure",
        "",
    ):
        cls = _classify_error(msg)
        assert cls is None, f"{msg!r} should not classify as transient"


def test_deterministic_subtype_unchanged_by_class_split() -> None:
    """Regression: the deterministic-subtype short-circuit still wins over
    BOTH transient classes (Fix 4 must keep working)."""
    # error_max_turns + timeout substring → still NOT transient
    assert (
        _classify_error("timeout after 600s", subtype="error_max_turns") is None
    )
    # error_max_turns + rate substring → still NOT transient
    assert _classify_error("rate limit", subtype="error_max_turns") is None
    # error_during_execution + claude exited → still NOT transient
    assert (
        _classify_error("claude exited 1", subtype="error_during_execution")
        is None
    )
    # error_max_tokens + 429 → still NOT transient
    assert _classify_error("429 too many", subtype="error_max_tokens") is None


# ── v0.5.4 Part 1C: Per-role timeout_s ─────────────────────────────────────


@pytest.mark.asyncio
async def test_role_timeout_s_overrides_inv_timeout(tmp_path: Path) -> None:
    """``role_timeout_s`` mapping is honored on the built invocation."""
    adapter = StubAdapter([_Result(text="A"), _Result(text="B")])
    client = AdapterLLMClient(
        adapter,
        cwd=tmp_path,
        timeout_s=600,  # client default
        role_timeout_s={"architect_b": 1200, "judge": 300},
    )
    await client.call(system="s", user="u", role="architect_b")
    await client.call(system="s", user="u", role="judge")

    assert adapter.calls[0].role == "architect_b"
    assert adapter.calls[0].timeout_s == 1200
    assert adapter.calls[1].role == "judge"
    assert adapter.calls[1].timeout_s == 300


@pytest.mark.asyncio
async def test_role_timeout_s_unknown_role_uses_default(tmp_path: Path) -> None:
    """A role not in ``role_timeout_s`` falls back to the client default."""
    adapter = StubAdapter([_Result(text="A")])
    client = AdapterLLMClient(
        adapter,
        cwd=tmp_path,
        timeout_s=600,
        role_timeout_s={"architect_b": 1200},
    )
    await client.call(system="s", user="u", role="judge")
    assert adapter.calls[0].timeout_s == 600


@pytest.mark.asyncio
async def test_no_role_timeout_dict_keeps_default(tmp_path: Path) -> None:
    """Without ``role_timeout_s``, the existing default flows through."""
    adapter = StubAdapter([_Result(text="A")])
    client = AdapterLLMClient(adapter, cwd=tmp_path, timeout_s=600)
    await client.call(system="s", user="u", role="critic_t")
    assert adapter.calls[0].timeout_s == 600


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
