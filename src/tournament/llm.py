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
    stop_after_attempt,
    wait_exponential,
)

from errors import AdapterError, TournamentError
from autologging import get_logger


_TRANSIENT_SUBSTRINGS = (
    "rate",
    "429",
    "overloaded",
    "529",
    "too many requests",
    "timeout",
    "timed out",
    "connection",
    "503",
)


class TransientError(AdapterError):
    """Retryable adapter failure (rate limit / transient network / overload)."""


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
    metadata: dict[str, Any] | None = None


def _build_invocation(
    role: str,
    system: str,
    user: str,
    cwd: Path,
    model: str | None,
    timeout_s: int,
) -> Any:
    """Build a Phase-2 AgentInvocation if available, else a duck-typed shim.

    The original implementation used separate system + user messages. Subscription
    CLIs accept a single prompt; we concatenate with a blank line between
    sections to preserve the semantic boundary.
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
            allowed_tools=[],  # text-only roles
            max_turns=1,
        )
    except Exception:
        # Fallback: the adapter in use may accept any object with the same fields.
        return _Invocation(
            role=role,
            prompt=prompt,
            cwd=cwd,
            model=model,
            timeout_s=timeout_s,
            allowed_tools=[],
            max_turns=1,
        )


def _classify_error(err: str | None, exc: BaseException | None = None) -> bool:
    """Return True if the error text indicates a transient failure."""
    text = (err or "") + " " + (str(exc) if exc else "")
    low = text.lower()
    return any(sub in low for sub in _TRANSIENT_SUBSTRINGS)


class AdapterLLMClient:
    """Wraps any adapter-like object behind the tournament's `LLMClient` protocol.

    Usage::

        client = AdapterLLMClient(adapter, cwd=repo_root)
        text = await client.call(system="...", user="...", role="critic_t")
    """

    def __init__(
        self,
        adapter: AdapterLike,
        cwd: Path,
        *,
        timeout_s: int = 600,
        max_attempts: int = 5,
    ) -> None:
        self._adapter = adapter
        self._cwd = cwd
        self._timeout_s = timeout_s
        self._max_attempts = max_attempts
        self._log = get_logger(component="tournament.llm")

    async def call(
        self,
        *,
        system: str,
        user: str,
        role: str,
        model: str | None = None,
    ) -> str:
        """Invoke the adapter with tenacity-backed retries on transient errors."""

        inv = _build_invocation(
            role=role,
            system=system,
            user=user,
            cwd=self._cwd,
            model=model,
            timeout_s=self._timeout_s,
        )

        @retry(
            stop=stop_after_attempt(self._max_attempts),
            wait=wait_exponential(multiplier=2, min=2, max=60),
            retry=retry_if_exception_type(TransientError),
            reraise=True,
        )
        async def _do_call() -> str:
            try:
                result = await self._adapter.execute(inv)
            except TransientError:
                raise
            except BaseException as exc:  # noqa: BLE001
                if _classify_error(None, exc):
                    self._log.info("transient_exception", role=role, err=str(exc))
                    raise TransientError(str(exc)) from exc
                raise TournamentError(
                    f"adapter.execute raised for role={role}: {exc}"
                ) from exc

            success = getattr(result, "success", True)
            error = getattr(result, "error", None)
            text = getattr(result, "text", None)
            if not success:
                if _classify_error(error):
                    self._log.info("transient_result", role=role, err=error)
                    raise TransientError(error or "transient adapter failure")
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
    "StubLLMClient",
    "TransientError",
]
