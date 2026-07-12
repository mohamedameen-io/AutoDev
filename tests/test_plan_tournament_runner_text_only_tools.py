"""v0.41.0 A4 (plan-tournament mirror): text-only tournament roles must NOT
carry Read in the PLAN tournament either.

Root cause (Run-3 / Run-4): ``critic_t`` / ``synthesizer`` are pure
text-in/text-out roles whose working content is inlined into their prompts by
the content handler, but :func:`plan_tournament_runner._build_role_overrides`
produced an empty tool list which
:meth:`AdapterLLMClient._resolve_allowed_tools` normalised to ``["Read"]``. At a
tiny turn budget a single speculative read consumed the only turn →
``error_max_turns`` → the whole plan-tournament branch died (all 3
plan-tournament branches failed this way in Run-3/Run-4 — the A4 "critic_t
error_max_turns" failure).

The phase-review runner already drops Read for these roles (the v0.41 fix in
:mod:`orchestrator.phase_review_runner`). These tests assert the plan-tournament
runner now mirrors that suppression:

  (a) :func:`plan_tournament_runner._build_role_overrides` yields an EMPTY
      ``allowed_tools`` list for every ``critic_t`` / ``synthesizer`` role,
      while leaving ``architect_b`` / ``judge`` on the non-suppressed branch;
      and
  (b) end-to-end through a real :class:`AdapterLLMClient` against a
      :class:`StubAdapter`, every ``critic_t`` / ``synthesizer`` invocation
      resolves to an EMPTY ``allowed_tools`` (Read dropped), while
      ``architect_b`` flows through its non-empty WS-5 registry grant
      (Read + Bash + recon) verbatim — proving the suppression is keyed off
      the text-only set, and that the WS-5 oracle-falsification grant reaches
      the subprocess invocation end-to-end (the empty->``["Read"]`` sentinel
      no longer applies to architect_b now that its registry tools are
      non-empty).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator import plan_tournament_runner as ptr
from orchestrator.plan_tournament_runner import _build_role_overrides
from tournament.llm import AdapterLLMClient, _TEXT_ONLY_NO_TOOL_ROLES

from stub_adapter import StubAdapter


# ---------------------------------------------------------------------------
# Helpers — minimal plan markdown + an Orchestrator with the plan tournament
# enabled and auto-disable cleared (so the runner actually constructs a client).
# ---------------------------------------------------------------------------


_SPEC_HASH = "0123456789abcdef"


def _plan_md(complexity: str | None = "medium") -> str:
    body = (
        "# Plan: Add foo(x)\n\n"
        "## Phase 1: Implement\n\n"
        "### Task 1.1: Write foo\n"
        "  - Description: Add a function foo.\n"
        "  - Files: foo.py\n"
        "  - Acceptance:\n"
        "    - [ ] function exists\n"
    )
    if complexity is None:
        return body
    return f"{body}\nCOMPLEXITY: {complexity}\n"


def _make_orch(cwd: Path, adapter: StubAdapter) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = True
    cfg.tournaments.plan.convergence_k = 1
    cfg.tournaments.plan.max_rounds = 2
    # Single judge keeps the run cheap + deterministic; tool resolution is
    # per-role and independent of cohort size.
    cfg.tournaments.plan.num_judges = 1
    cfg.tournaments.auto_disable_for_models = []
    cfg.tournaments.plan.auto_disable_for_models = []
    cfg.tournaments.impl.enabled = False
    cfg.tournaments.phase_review.enabled = False
    registry = build_registry(cfg)
    return Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-plan-a4-text-only",
    )


def _a_wins_handler(_inv):  # type: ignore[no-untyped-def]
    """Return parseable text per role so the plan tournament converges on A.

    The critic / synthesizer responses are produced WITHOUT any filesystem read
    — derived purely from the inline prompt — which is exactly the behaviour
    Read removal is meant to guarantee.
    """
    from adapters.types import AgentResult

    role = _inv.role
    if role == "critic_t":
        return AgentResult(
            success=True,
            text="- the plan looks coherent; no structural gaps found",
            duration_s=0.01,
        )
    if role == "architect_b":
        # Return the plan markdown unchanged so the B variant stays parseable.
        return AgentResult(
            success=True,
            text=_inv.prompt,
            duration_s=0.01,
        )
    if role == "synthesizer":
        return AgentResult(
            success=True,
            text=_inv.prompt,
            duration_s=0.01,
        )
    if role == "judge":
        # Rank the incumbent (A) first → convergence_k=1 converges on pass 1.
        return AgentResult(
            success=True,
            text="A is the strongest.\nRANKING: 1, 2, 3",
            duration_s=0.01,
        )
    return AgentResult(success=True, text=f"[stub:{role}]", duration_s=0.01)


# ---------------------------------------------------------------------------
# (a) _build_role_overrides drops Read for text-only roles
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_role_overrides_drops_read_for_text_only_roles(
    tmp_path: Path,
) -> None:
    """``plan_tournament_runner._build_role_overrides`` FORCES ``[]`` for
    critic_t / synthesizer even when their registry specs carry tools, while
    leaving architect_b / judge on the non-suppressed branch (their registry
    tools pass through verbatim).

    The registry specs are deliberately given NON-empty tools here so the test
    genuinely exercises the text-only suppression at the runner boundary: with
    the old ``list(spec.tools) if spec.tools else []`` line the text-only roles
    would keep ``["Read", "Grep"]``; with the A4 suppression they collapse to
    ``[]``.
    """
    orch = _make_orch(tmp_path, StubAdapter({}))

    # Inject non-empty tools into every tournament role's registry spec so the
    # suppression is observable (an empty registry tool list would make the
    # text-only and non-text-only branches indistinguishable).
    for role in ("critic_t", "synthesizer", "architect_b", "judge"):
        spec = orch.registry[role]
        orch.registry[role] = spec.model_copy(update={"tools": ["Read", "Grep"]})

    _max_turns, allowed_tools, _timeout, _effort = _build_role_overrides(
        orch, "medium"
    )

    # Text-only roles are FORCED empty despite carrying registry tools.
    for role in _TEXT_ONLY_NO_TOOL_ROLES:
        assert allowed_tools[role] == [], (
            f"{role} should be forced to an empty tool list at the runner "
            f"boundary despite registry tools {['Read', 'Grep']!r}"
        )
    # Non-suppressed roles pass their registry tools through unchanged.
    assert allowed_tools["architect_b"] == ["Read", "Grep"]
    assert allowed_tools["judge"] == ["Read", "Grep"]


@pytest.mark.asyncio
async def test_architect_b_resolves_read_and_bash_end_to_end(
    tmp_path: Path,
) -> None:
    """WS-5: through the REAL resolution path (registry grant ->
    ``_build_role_overrides`` -> ``AdapterLLMClient._resolve_allowed_tools``),
    ``architect_b`` resolves to a tool set that includes Bash and Read and is
    NOT the bare ``["Read"]`` sentinel.

    This is the RED-pre-fix behavioral assertion the WS-5 spec pins: with the
    old ``AGENT_TOOL_MAP["architect_b"] = []`` the registry produced empty
    tools, ``_build_role_overrides`` yielded ``[]``, and
    ``_resolve_allowed_tools`` normalised that to ``["Read"]`` — so the critic
    could never run a reproduction. The grant must flow the whole way through.
    """
    orch = _make_orch(tmp_path, StubAdapter({}))

    # No spec injection — use the real registry grant resolved from tool_map.
    _max_turns, allowed_tools, _timeout, _effort = _build_role_overrides(
        orch, "medium"
    )
    client = AdapterLLMClient(orch.adapter, cwd=orch.cwd, role_allowed_tools=allowed_tools)

    resolved = client._resolve_allowed_tools("architect_b")
    assert resolved is not None
    assert "Bash" in resolved and "Read" in resolved, (
        f"architect_b resolved to {resolved!r}; WS-5 requires Read + Bash"
    )
    assert resolved != ["Read"], (
        "architect_b must NOT collapse to the bare ['Read'] sentinel — the "
        "non-empty registry grant must suppress it"
    )


# ---------------------------------------------------------------------------
# (b) end-to-end: no Read on critic/synth invocations + verdict reached
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_tournament_critic_synth_invocations_carry_no_read(
    tmp_path: Path,
) -> None:
    """Run the real plan tournament. Assert every critic_t / synthesizer
    subprocess invocation was built with an EMPTY ``allowed_tools`` (no Read),
    while architect_b flows through its non-empty WS-5 registry grant
    (Read + Bash) — proving the suppression is keyed off the text-only set, and
    that the WS-5 grant reaches the invocation end-to-end."""
    adapter = StubAdapter(
        {
            "critic_t": _a_wins_handler,
            "architect_b": _a_wins_handler,
            "synthesizer": _a_wins_handler,
            "judge": _a_wins_handler,
        }
    )
    orch = _make_orch(tmp_path, adapter)

    final_md = await ptr.run_plan_tournament(
        orch, _plan_md("medium"), "spec text", spec_hash=_SPEC_HASH
    )

    # A converged plan markdown was returned (no exception).
    assert isinstance(final_md, str) and final_md

    # Every text-only-role invocation dropped Read entirely.
    text_only_calls = [
        c for c in adapter.calls if c.role in _TEXT_ONLY_NO_TOOL_ROLES
    ]
    assert text_only_calls, "expected at least one critic_t/synthesizer call"
    for c in text_only_calls:
        assert c.allowed_tools == [], (
            f"{c.role} invocation carried tools={c.allowed_tools!r}; "
            f"text-only roles must resolve to an EMPTY tool set (no Read)"
        )
        assert c.allowed_tools != ["Read"]

    # architect_b is NOT suppressed AND (WS-5) now carries a non-empty registry
    # grant (Read + Bash + recon), so it flows through verbatim — the
    # empty->["Read"] sentinel no longer applies. This proves the WS-5
    # oracle-falsification grant reaches the actual subprocess invocation
    # end-to-end (registry -> _build_role_overrides -> _resolve_allowed_tools).
    architect_calls = [c for c in adapter.calls if c.role == "architect_b"]
    assert architect_calls, "expected at least one architect_b call"
    for c in architect_calls:
        assert c.allowed_tools is not None
        assert "Bash" in c.allowed_tools and "Read" in c.allowed_tools, (
            f"architect_b invocation carried {c.allowed_tools!r}; WS-5 must "
            f"grant Read + Bash so the critic can falsify the acceptance oracle"
        )
        assert c.allowed_tools != ["Read"], (
            "architect_b resolving to bare ['Read'] means the WS-5 Bash grant "
            "did not flow end-to-end"
        )
