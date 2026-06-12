"""Adapter-backed LLM wrapper for tournament role calls.

Phase 5 deliberately does NOT import `adapters.*` because Phase 2 may
still be in flight. We instead declare a minimal `AdapterLike` protocol that
matches whatever Phase 2 ships. Any object with `async execute(inv) -> result`
where `inv` is constructible from `role/prompt/cwd/...` fields satisfies it.

Retry semantics:
    - Transient errors (rate limits, overloaded, 429/529-ish) retry with
      exponential backoff via `tenacity`.
    - Non-transient errors (parsing failure, permission denied, bad input)
      do NOT retry — they propagate as `TournamentError`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    wait_exponential,
    wait_random,
)

from errors import AdapterError, TournamentError
from autologging import get_logger
from tournament.errors import AuthenticationFailedError


# Substrings whose corresponding failures are normal-cost transients —
# fast to retry, so we use the default ``max_attempts`` budget (typically 5).
_TRANSIENT_SUBSTRINGS = (
    "rate",
    "429",
    "overloaded",
    "529",
    "too many requests",
    "connection",
    "503",
    # Subprocess process-death patterns (see Tier 1A in claude_code.py).
    # NOTE: these are matched lowercase via _classify_error.
    "claude exited",
    "empty stderr",
    "broken pipe",
)


# Substrings whose corresponding failures are EXPENSIVE transients — the
# retry itself takes hundreds of seconds, so we cap them at
# ``max_attempts_expensive`` (typically 3) to bound total wall-clock burn.
# Without this cap a 600s timeout retried 5x = ~50 min silent loss (QNX bug).
_EXPENSIVE_TRANSIENT_SUBSTRINGS = (
    "timeout",
    "timed out",
)


# Subtypes that the CLI reports for deterministic failures — retrying with the
# same prompt cannot help. When :class:`AgentResult.subtype` matches one of
# these, ``_classify_error`` short-circuits transient classification and the
# call is wrapped in a :class:`TournamentError` (or, for ``auth_failed``, the
# typed :class:`AuthenticationFailedError`) immediately.
#
# v0.28.0 Bug 2 added ``auth_failed`` (401/403 — bad credentials, every
# subsequent call would also fail until the operator refreshes the token)
# and ``client_error`` (4xx other than 401/403/429 — bad-request style; the
# same prompt cannot succeed). ``rate_limited`` (429) and ``server_error``
# (5xx) deliberately stay OUT — those classify as transient via the explicit
# subtype branches in :func:`_classify_error` (the substring fallback for
# "rate" / "503" still matches too, but the typed branch is authoritative).
_DETERMINISTIC_SUBTYPES = frozenset(
    {
        "error_max_turns",
        "error_max_tokens",
        "error_during_execution",
        "auth_failed",
        "client_error",
    }
)


class TransientError(AdapterError):
    """Retryable adapter failure (rate limit / transient network / overload)."""


class ExpensiveTransientError(TransientError):
    """Retryable but expensive failure (e.g. subprocess timeout).

    Subclassed from :class:`TransientError` so existing
    ``retry_if_exception_type(TransientError)`` predicates still match, but
    the retry loop's stop predicate inspects the concrete class to apply a
    smaller attempt budget (``max_attempts_expensive``).
    """


@runtime_checkable
class AdapterLike(Protocol):
    """Duck-typed view of Phase 2's `PlatformAdapter`.

    Only the `execute` method is required; we don't depend on its full
    signature. Returned object must expose `.text`, `.success`, `.error` and
    ideally `.duration_s`.
    """

    async def execute(self, inv: Any) -> Any: ...


@dataclass
class _Invocation:
    """Plain dataclass fallback when Phase 2's pydantic AgentInvocation is absent.

    Phase 2 adapters constructed from its own `AgentInvocation` pydantic model;
    duck-typing means this shim works too in tests.
    """

    role: str
    prompt: str
    cwd: Path
    model: str | None = None
    timeout_s: int = 600
    allowed_tools: list[str] | None = None
    max_turns: int = 1
    effort: str | None = None
    metadata: dict[str, Any] | None = None


def _build_invocation(
    role: str,
    system: str,
    user: str,
    cwd: Path,
    model: str | None,
    timeout_s: int,
    max_turns: int = 1,
    allowed_tools: list[str] | None = None,
    effort: str | None = None,
) -> Any:
    """Build a Phase-2 AgentInvocation if available, else a duck-typed shim.

    The original implementation used separate system + user messages. Subscription
    CLIs accept a single prompt; we concatenate with a blank line between
    sections to preserve the semantic boundary.

    ``max_turns`` and ``allowed_tools`` default to text-only-role conventions
    (1 turn, no tool restriction). :class:`AdapterLLMClient` overrides them
    per-role from the ``role_max_turns`` / ``role_allowed_tools`` maps.

    ``effort`` (Claude Code ``--effort``) defaults to ``None`` (inherit
    user-global). :class:`AdapterLLMClient` resolves it per-role from the
    ``role_effort`` map.
    """
    prompt = f"{system}\n\n{user}"
    try:
        from adapters.types import AgentInvocation  # type: ignore

        return AgentInvocation(
            role=role,
            prompt=prompt,
            cwd=cwd,
            model=model,
            timeout_s=timeout_s,
            allowed_tools=allowed_tools,
            max_turns=max_turns,
            effort=effort,
        )
    except Exception:
        # Fallback: the adapter in use may accept any object with the same fields.
        return _Invocation(
            role=role,
            prompt=prompt,
            cwd=cwd,
            model=model,
            timeout_s=timeout_s,
            allowed_tools=allowed_tools,
            max_turns=max_turns,
            effort=effort,
        )


def _classify_error(
    err: str | None,
    exc: BaseException | None = None,
    *,
    subtype: str | None = None,
) -> type[TransientError] | None:
    """Return the transient class for a given error string, or ``None``.

    Returns:
        - :class:`ExpensiveTransientError` if the message matches a substring
          in ``_EXPENSIVE_TRANSIENT_SUBSTRINGS`` (currently: timeout-related).
        - :class:`TransientError` for normal transients in
          ``_TRANSIENT_SUBSTRINGS`` (rate limits, 429/529, connection, etc.).
        - ``None`` for non-transient or empty failures.

    Deterministic CLI failure subtypes (``subtype in _DETERMINISTIC_SUBTYPES``
    — e.g. ``error_max_turns``, ``auth_failed``) take precedence and force
    ``None`` regardless of the error string. This prevents the retry loop
    from burning attempts on a failure that is guaranteed to repeat (Fix 4 —
    must keep working across BOTH transient classes after the v0.5.4 split).

    v0.28.0 Bug 2: typed transient subtypes (``rate_limited`` → 429,
    ``server_error`` → 5xx) take precedence over the substring fallback so
    a malformed / non-substring-matching error message still routes to the
    right retry budget. ``ExpensiveTransientError`` is reserved for the
    timeout substrings — neither typed subtype lands there today.
    """
    if subtype is not None and subtype in _DETERMINISTIC_SUBTYPES:
        return None
    if subtype == "rate_limited":
        return TransientError
    if subtype == "server_error":
        return TransientError
    text = (err or "") + " " + (str(exc) if exc else "")
    low = text.lower()
    # Expensive transients are a strict subset of "should retry" but with a
    # smaller budget; check them first so a literal "timeout" message is
    # routed to the cap.
    if any(sub in low for sub in _EXPENSIVE_TRANSIENT_SUBSTRINGS):
        return ExpensiveTransientError
    if any(sub in low for sub in _TRANSIENT_SUBSTRINGS):
        return TransientError
    return None


class AdapterLLMClient:
    """Wraps any adapter-like object behind the tournament's `LLMClient` protocol.

    Usage::

        client = AdapterLLMClient(
            adapter,
            cwd=repo_root,
            role_max_turns={"architect_b": 5},
            role_allowed_tools={"architect_b": []},
        )
        text = await client.call(system="...", user="...", role="critic_t")

    Per-role overrides:
        ``role_max_turns`` and ``role_allowed_tools`` are optional dicts keyed
        by tournament role name (``critic_t`` / ``architect_b`` / ``synthesizer``
        / ``judge``). When a role appears in the map, its value is used to
        construct the :class:`~adapters.types.AgentInvocation` for that call.
        Roles not in the map fall back to defaults (``max_turns=1``,
        ``allowed_tools=None``) — the pre-Fix-2/3 behavior.

        Empty list (``[]``) in ``role_allowed_tools`` is normalized to
        ``["Read"]``: the Claude CLI's variadic ``--allowed-tools`` flag is
        skipped on falsy values, which would silently allow all tools.
        ``Read`` is a benign read-only sentinel for text-only roles.
    """

    def __init__(
        self,
        adapter: AdapterLike,
        cwd: Path,
        *,
        timeout_s: int = 600,
        max_attempts: int = 5,
        max_attempts_expensive: int = 3,
        role_max_turns: dict[str, int] | None = None,
        role_allowed_tools: dict[str, list[str] | None] | None = None,
        role_effort: dict[str, str] | None = None,
        role_timeout_s: dict[str, int] | None = None,
        role_model_overrides: dict[str, str] | None = None,
    ) -> None:
        self._adapter = adapter
        self._cwd = cwd
        self._timeout_s = timeout_s
        self._max_attempts = max_attempts
        self._max_attempts_expensive = max_attempts_expensive
        self._role_max_turns = role_max_turns
        self._role_allowed_tools = role_allowed_tools
        self._role_effort = role_effort
        self._role_timeout_s = role_timeout_s
        # v0.14.0: per-role model override map for hetero-model branches.
        # When non-None and the role is in the map, the override replaces
        # the ``model`` arg passed to :meth:`call`. Empty dict / None
        # preserves legacy single-model-per-tournament behavior.
        self._role_model_overrides = role_model_overrides
        self._log = get_logger(component="tournament.llm")

    def _resolve_max_turns(self, role: str) -> int:
        if self._role_max_turns is None:
            return 1
        return self._role_max_turns.get(role, 1)

    def _resolve_allowed_tools(self, role: str) -> list[str] | None:
        if self._role_allowed_tools is None:
            return None
        if role not in self._role_allowed_tools:
            return None
        configured = self._role_allowed_tools[role]
        # Empty list = "no tools" intent. The Claude CLI's variadic
        # --allowed-tools flag is omitted on falsy values, so we substitute a
        # benign read-only sentinel that actually restricts the toolset.
        if configured is not None and len(configured) == 0:
            return ["Read"]
        return configured

    def _resolve_effort(self, role: str) -> str | None:
        """Return the per-role ``--effort`` hint, or ``None`` to inherit.

        Roles absent from the ``role_effort`` map (or the map being ``None``)
        return ``None`` — the adapter omits the flag and the Claude CLI
        inherits the user-global default in ``~/.claude/settings.json``.
        """
        if self._role_effort is None:
            return None
        return self._role_effort.get(role)

    def _resolve_timeout_s(self, role: str) -> int:
        """Return the per-role timeout in seconds, or the client default.

        Roles absent from the ``role_timeout_s`` map fall back to
        ``self._timeout_s`` (the AdapterLLMClient ctor default, typically 600s).
        Used so complex-plan architects/synthesizers can run longer without
        affecting cheap roles like the judge.
        """
        if self._role_timeout_s is not None and role in self._role_timeout_s:
            return self._role_timeout_s[role]
        return self._timeout_s

    @property
    def last_pid(self) -> int | None:
        """Most recent subprocess PID from the underlying adapter, or None.

        v0.10.0: forwards the adapter's ``last_pid`` for use by
        :meth:`tournament.core.Tournament._run_judges`'s per-pass adaptive
        ratcheting. Defined as a property so the tournament code can read
        a fresh value on each call without holding a reference to the
        adapter directly. Returns ``None`` if the wrapped adapter doesn't
        expose ``last_pid`` (e.g. ``StubAdapter``) — graceful degradation.
        """
        return getattr(self._adapter, "last_pid", None)

    async def call(
        self,
        *,
        system: str,
        user: str,
        role: str,
        model: str | None = None,
    ) -> str:
        """Invoke the adapter with tenacity-backed retries on transient errors.

        Two retry budgets are dispatched based on classification:
            - ``max_attempts`` for normal transients (cheap to retry).
            - ``max_attempts_expensive`` for :class:`ExpensiveTransientError`
              (e.g. timeouts — capped to bound silent wall-clock burn).

        The dispatch is implemented via a custom ``stop`` callable that
        consults the failing exception's class on each attempt.
        """

        # v0.14.0: per-role model override for hetero-model branches. The
        # incoming ``model`` is the cohort default (typically the judge
        # model resolved in plan_tournament_runner). When the role has a
        # branch-specific override, swap to it.
        effective_model = model
        if (
            self._role_model_overrides is not None
            and role in self._role_model_overrides
        ):
            effective_model = self._role_model_overrides[role]

        inv = _build_invocation(
            role=role,
            system=system,
            user=user,
            cwd=self._cwd,
            model=effective_model,
            timeout_s=self._resolve_timeout_s(role),
            max_turns=self._resolve_max_turns(role),
            allowed_tools=self._resolve_allowed_tools(role),
            effort=self._resolve_effort(role),
        )

        max_attempts = self._max_attempts
        max_attempts_expensive = self._max_attempts_expensive

        def _stop_dispatch(retry_state: Any) -> bool:
            """Stop policy: cap expensive transients sooner than normal ones.

            ``retry_state.attempt_number`` is 1-based. We stop AFTER the
            attempt-number reaches the relevant cap.
            """
            attempt = retry_state.attempt_number
            outcome = retry_state.outcome
            # If outcome is None or did not fail, defer to the larger budget
            # (tenacity won't actually call this on success — kept defensive).
            if outcome is None or not outcome.failed:
                return attempt >= max_attempts
            exc = outcome.exception()
            if isinstance(exc, ExpensiveTransientError):
                return attempt >= max_attempts_expensive
            return attempt >= max_attempts

        @retry(
            stop=_stop_dispatch,
            # v0.39.0 B4: add 0-2s random jitter on top of the exponential
            # backoff so parallel agents re-fire on de-synced clocks,
            # breaking the thundering-herd that drives 429/529 storms.
            wait=wait_exponential(multiplier=2, min=2, max=60) + wait_random(0, 2),
            retry=retry_if_exception_type(TransientError),
            reraise=True,
        )
        async def _do_call() -> str:
            try:
                result = await self._adapter.execute(inv)
            except TransientError:
                raise
            except BaseException as exc:  # noqa: BLE001
                cls = _classify_error(None, exc)
                if cls is not None:
                    self._log.info(
                        "transient_exception",
                        role=role,
                        err=str(exc),
                        cls=cls.__name__,
                    )
                    raise cls(str(exc)) from exc
                raise TournamentError(
                    f"adapter.execute raised for role={role}: {exc}"
                ) from exc

            success = getattr(result, "success", True)
            error = getattr(result, "error", None)
            text = getattr(result, "text", None)
            subtype = getattr(result, "subtype", None)
            if not success:
                if subtype in _DETERMINISTIC_SUBTYPES:
                    self._log.warning(
                        "deterministic_subtype",
                        role=role,
                        subtype=subtype,
                        err=error,
                    )
                    # v0.28.0 Bug 2: ``auth_failed`` (401/403 from the
                    # upstream API) propagates as the typed
                    # :class:`AuthenticationFailedError` so the orchestrator's
                    # top-level loop can catch it by type and abort the
                    # phase loop without thrashing every subsequent task
                    # against the same dead credential. All other
                    # deterministic subtypes (max_turns, max_tokens,
                    # error_during_execution, client_error) stay on the
                    # generic :class:`TournamentError` path — those are
                    # task-local and the loop should continue with the
                    # next task.
                    if subtype == "auth_failed":
                        raise AuthenticationFailedError(
                            f"auth_failed for role={role}: {error}"
                        )
                    raise TournamentError(
                        f"non-retryable subtype={subtype} for role={role}: {error}"
                    )
                cls = _classify_error(error, subtype=subtype)
                if cls is not None:
                    self._log.info(
                        "transient_result",
                        role=role,
                        err=error,
                        cls=cls.__name__,
                    )
                    raise cls(error or "transient adapter failure")
                raise TournamentError(
                    f"adapter returned success=False for role={role}: {error}"
                )
            if text is None:
                raise TournamentError(f"adapter result had no .text for role={role}")
            return str(text)

        try:
            return await _do_call()
        except RetryError as exc:  # pragma: no cover — reraise=True bypasses this
            raise TournamentError(f"exhausted retries for role={role}: {exc}") from exc


class StubLLMClient:
    """Deterministic LLM client for tests.

    Two modes:
        - Callback mode: pass `fn(role, system, user) -> str`.
        - Dict mode: pass `responses={role: text}` or `responses={(role, N): text}`
          where N is the call count for that role (1-based).

    Records every call in `self.calls` for assertions.
    """

    def __init__(
        self,
        fn: Callable[[str, str, str], str] | None = None,
        responses: dict[Any, str] | None = None,
        default: str = "STUB_RESPONSE",
    ) -> None:
        if fn is None and responses is None:
            raise ValueError("StubLLMClient requires either fn or responses")
        self._fn = fn
        self._responses = responses or {}
        self._default = default
        self._role_counts: dict[str, int] = {}
        self.calls: list[dict[str, Any]] = []

    async def call(
        self,
        *,
        system: str,
        user: str,
        role: str,
        model: str | None = None,
    ) -> str:
        self._role_counts[role] = self._role_counts.get(role, 0) + 1
        n = self._role_counts[role]
        self.calls.append(
            {"role": role, "system": system, "user": user, "model": model, "n": n}
        )
        if self._fn is not None:
            return self._fn(role, system, user)
        # Key preference: (role, n) > role > default
        if (role, n) in self._responses:
            return self._responses[(role, n)]
        if role in self._responses:
            return self._responses[role]
        return self._default


__all__ = [
    "AdapterLike",
    "AdapterLLMClient",
    "AuthenticationFailedError",
    "ExpensiveTransientError",
    "StubLLMClient",
    "TransientError",
]
