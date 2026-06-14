"""In-process fake adapter used by Phase-4 orchestrator tests.

The adapter accepts a mapping of ``role -> AgentResult`` (or role -> callable
that returns an ``AgentResult`` given an ``AgentInvocation``). Each call is
recorded in ``self.calls`` for assertions.

No subprocesses are spawned; tests run entirely in-process.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Union

from adapters.base import PlatformAdapter
from adapters.types import AgentInvocation, AgentResult, AgentSpec


StubHandler = Union[
    AgentResult,
    list[AgentResult],
    Callable[[AgentInvocation], AgentResult],
]


class StubAdapter(PlatformAdapter):
    """Test double for :class:`src.adapters.base.PlatformAdapter`.

    ``responses`` may contain:

      - an ``AgentResult`` to return every time the role is invoked;
      - a list of ``AgentResult`` popped FIFO per call for that role;
      - a callable ``fn(inv) -> AgentResult`` for full control.
    """

    name = "stub"

    def __init__(self, responses: dict[str, StubHandler]) -> None:
        self._responses = dict(responses)
        self.calls: list[AgentInvocation] = []
        self._counters: dict[str, int] = {}

    async def init_workspace(self, cwd: Path, agents: list[AgentSpec]) -> None:
        # No-op for stub.
        return

    async def execute(self, inv: AgentInvocation) -> AgentResult:
        self.calls.append(inv)
        self._counters[inv.role] = self._counters.get(inv.role, 0) + 1
        handler = self._responses.get(inv.role)
        if handler is None:
            # v0.17.0 S1: role-aware fallback for ``critic_drift_verifier``.
            # Drift-verifier output is parsed for ``VERDICT: APPROVED|REJECTED``;
            # the legacy ``[stub:{role}] default-ok`` text is unparseable and
            # would crash drift-verifier-enabled tests that don't explicitly
            # stub the role. Returning an APPROVED verdict here mirrors the
            # "no drift" default (the gate's safe-mode position).
            if inv.role == "critic_drift_verifier":
                return AgentResult(
                    success=True,
                    text="VERDICT: APPROVED\n",
                    duration_s=0.01,
                )
            # v0.20.0 C2: extended-scope critic review. When a stub
            # adapter test triggers an EXTENDED_SCOPE_REVIEW (recognized
            # by the constraint substring in the prompt), default to
            # approval so existing tests don't need to stub the role.
            # Negative tests stub critic_sounding_board explicitly.
            if (
                inv.role == "critic_sounding_board"
                and "EXTENDED_SCOPE_REVIEW" in (inv.prompt or "")
            ):
                return AgentResult(
                    success=True,
                    text="RESOLUTION: approved-extended-scope\n",
                    duration_s=0.01,
                )
            # v0.21.0 A2: synthesizer with diff input → diff output. The
            # multi-branch impl meta-merge calls the synthesizer role
            # with N candidate diffs and expects a fenced ``diff`` block
            # back. Default fallback emits a no-op merged diff so tests
            # that don't explicitly stub synthesizer don't crash.
            if inv.role == "synthesizer" and "CANDIDATE 1" in (
                inv.prompt or ""
            ):
                return AgentResult(
                    success=True,
                    text=(
                        "Stub synthesizer fallback merged diff:\n\n"
                        "```diff\ndiff --git a/stub.txt b/stub.txt\n"
                        "@@\n+stub-merged\n```\n"
                    ),
                    duration_s=0.01,
                )
            # ADR-0044: framing defaults to a safe local_defect classification so
            # tests that don't explicitly stub the role degrade conservatively
            # (single local_patch, no panel) rather than crash the parser.
            if inv.role == "framing":
                return AgentResult(
                    success=True,
                    text=(
                        "```framing\n"
                        "CLASSIFICATION: local_defect\n"
                        "CONFIDENCE: 0.0\n"
                        "HYPOTHESIS_CHALLENGED: stub default\n"
                        "SIGNALS_FIRED: none\n"
                        "```\n"
                    ),
                    duration_s=0.01,
                )
            # ADR-0044: altitude_judge default ranking covers N=2 and N=3 (extra
            # digits are filtered by parse_ranking against the dynamic valid_labels).
            if inv.role == "altitude_judge":
                return AgentResult(
                    success=True,
                    text="```ranking\nRANKING: 1 2 3\n```\n",
                    duration_s=0.01,
                )
            return AgentResult(
                success=True,
                text=f"[stub:{inv.role}] default-ok",
                duration_s=0.01,
            )
        if callable(handler):
            return handler(inv)
        if isinstance(handler, list):
            idx = self._counters[inv.role] - 1
            if idx >= len(handler):
                # Reuse the last entry for "and then always return this".
                return handler[-1]
            return handler[idx]
        return handler

    async def healthcheck(self) -> tuple[bool, str]:
        return True, "stub"

    # --- Test helpers ---

    def count(self, role: str) -> int:
        return self._counters.get(role, 0)

    def prompts_for(self, role: str) -> list[str]:
        return [c.prompt for c in self.calls if c.role == role]


def ok(text: str, **kwargs: Any) -> AgentResult:
    """Convenience builder for a successful :class:`AgentResult`."""
    return AgentResult(success=True, text=text, duration_s=0.01, **kwargs)


def fail(error: str, **kwargs: Any) -> AgentResult:
    """Convenience builder for a failed :class:`AgentResult`."""
    return AgentResult(success=False, text="", duration_s=0.01, error=error, **kwargs)
