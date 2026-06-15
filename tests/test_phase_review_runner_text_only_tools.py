"""v0.41.0 A4: text-only tournament roles must NOT carry Read.

Root cause (Run-3): ``critic_t`` / ``synthesizer`` are pure text-in/text-out
roles whose working content is inlined into their prompts by the content
handler, but :func:`phase_review_runner._build_role_overrides` produced an
empty tool list which :meth:`AdapterLLMClient._resolve_allowed_tools`
normalised to ``["Read"]``. At a tiny turn budget a single speculative read
consumed the only turn → ``error_max_turns`` → the whole tournament branch
died (all 3 plan-tournament branches failed this way).

These tests run the REAL phase-review tournament through a real
:class:`AdapterLLMClient` against a :class:`StubAdapter` so we can assert:

  (a) every ``critic_t`` / ``synthesizer`` invocation resolves to an EMPTY
      ``allowed_tools`` list (Read dropped), while ``architect_b`` keeps its
      Read sentinel; and
  (b) a multi-branch phase-review tournament runs to a verdict with NO
      branch failures attributable to critic/synthesizer turn exhaustion
      (the StubAdapter returns content without performing any read).
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

import pytest

from adapters.types import AgentInvocation, AgentResult
from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator import phase_review_runner as prr
from orchestrator.phase_review_runner import _build_role_overrides
from state.plan_manager import PlanManager
from state.schemas import AcceptanceCriterion, Phase, Plan, Task
from tournament.llm import _TEXT_ONLY_NO_TOOL_ROLES

from stub_adapter import StubAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan() -> Plan:
    return Plan(
        plan_id="p-a4",
        spec_hash="0123456789abcdef",
        phases=[
            Phase(
                id="1",
                title="Investigate",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="t",
                        description="d",
                        complexity="medium",
                        status="complete",
                    ),
                ],
                acceptance=[
                    AcceptanceCriterion(id="ph-ac-1", description="all tests pass"),
                ],
                baseline_commit="aaaa1111",
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
        complexity="medium",
    )


def _make_orch(cwd: Path, adapter: StubAdapter) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.phase_review.enabled = True
    cfg.tournaments.auto_disable_for_models = []
    cfg.tournaments.phase_review.auto_disable_for_models = []
    cfg.tournaments.impl.enabled = False
    cfg.tournaments.plan.enabled = False
    # Single judge keeps the run cheap + deterministic; tool resolution is
    # per-role and independent of cohort size.
    cfg.tournaments.phase_review.num_judges = 1
    registry = build_registry(cfg)
    return Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-a4-text-only",
    )


def _a_wins_handler(_inv: AgentInvocation) -> AgentResult:
    """Return parseable text per role so the tournament converges on A.

    Crucially the critic / synthesizer responses are produced WITHOUT any
    filesystem read — they are derived purely from the inline prompt — which
    is exactly the behaviour Read removal is meant to guarantee.
    """
    role = _inv.role
    if role == "critic_t":
        return AgentResult(
            success=True,
            text="- the diff looks coherent; no acceptance gaps found",
            duration_s=0.01,
        )
    if role == "architect_b":
        return AgentResult(
            success=True,
            text="- no corrective tasks needed; phase is compliant",
            duration_s=0.01,
        )
    if role == "synthesizer":
        return AgentResult(
            success=True,
            text="- no corrective tasks needed (synthesis agrees)",
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


@pytest.fixture(autouse=True)
def _stub_git_diff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Don't shell out for the diff — return a small inline diff."""
    monkeypatch.setattr(
        prr,
        "_git_diff_range",
        lambda cwd, a, b: (
            "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n+def x(): return 1\n"
        ),
    )


# ---------------------------------------------------------------------------
# (a) _build_role_overrides drops Read for text-only roles
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_role_overrides_drops_read_for_text_only_roles(
    tmp_path: Path,
) -> None:
    """``_build_role_overrides`` yields ``[]`` for critic_t / synthesizer and
    leaves architect_b alone (still ``[]`` from its registry, which the client
    normalises to ``["Read"]`` — not affected by the A4 suppression)."""
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_plan())
    orch = _make_orch(tmp_path, StubAdapter({}))

    _max_turns, allowed_tools, _timeout, _effort = _build_role_overrides(
        orch, plan_complexity="medium"
    )
    for role in _TEXT_ONLY_NO_TOOL_ROLES:
        assert allowed_tools[role] == [], (
            f"{role} should resolve to an empty tool list at the runner boundary"
        )
    # architect_b is present and NOT forced empty by the A4 path (its registry
    # tools happen to be empty here, but the runner did not key off the
    # text-only set for it).
    assert "architect_b" in allowed_tools


# ---------------------------------------------------------------------------
# (b) end-to-end: no Read on critic/synth invocations + verdict reached
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_branch_critic_synth_invocations_carry_no_read(
    tmp_path: Path,
) -> None:
    """Run the real single-branch phase-review tournament. Assert every
    critic_t / synthesizer subprocess invocation was built with an EMPTY
    ``allowed_tools`` (no Read), and the tournament reached a verdict."""
    adapter = StubAdapter(
        {
            "critic_t": _a_wins_handler,
            "architect_b": _a_wins_handler,
            "synthesizer": _a_wins_handler,
            "judge": _a_wins_handler,
        }
    )
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_plan())
    orch = _make_orch(tmp_path, adapter)

    plan = await orch.plan_manager.load()
    phase = plan.phases[0]  # type: ignore[union-attr]
    outcome = await prr.run_phase_review_tournament(
        orch, phase, "aaaa1111", "bbbb2222", spec_md="my spec"
    )

    # A verdict was produced (no exception, history populated).
    assert outcome.winner in ("A", "B", "AB")
    assert outcome.history, "tournament produced no passes"

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


@pytest.mark.asyncio
async def test_multi_branch_runs_to_verdict_without_branch_failures(
    tmp_path: Path,
) -> None:
    """A 3-branch phase-review tournament runs to a meta-merged verdict with
    NO branch failures. With Read dropped + inline content the critic /
    synthesizer never hit error_max_turns, so every branch survives and the
    survivor floor is met."""
    ledger_ops: list[str] = []

    def _capturing_handler(_inv: AgentInvocation) -> AgentResult:
        return _a_wins_handler(_inv)

    adapter = StubAdapter(
        {
            "critic_t": _capturing_handler,
            "architect_b": _capturing_handler,
            "synthesizer": _capturing_handler,
            "judge": _capturing_handler,
        }
    )
    pm = PlanManager(tmp_path, session_id="sess-init")
    await pm.init_plan(_mk_plan())
    orch = _make_orch(tmp_path, adapter)

    # Spy on ledger ops to assert no branch_failed-style meta-merge shortfall.
    orig_append = orch.plan_manager.ledger_append

    async def _spy(op: str, payload: dict[str, Any]) -> Any:
        ledger_ops.append(op)
        return await orig_append(op=op, payload=payload)

    orch.plan_manager.ledger_append = _spy  # type: ignore[assignment]

    plan = await orch.plan_manager.load()
    phase = plan.phases[0]  # type: ignore[union-attr]

    outcome = await prr.run_multi_branch_phase_review_tournament(
        orch,
        phase,
        baseline_commit="aaaa1111",
        tip_commit="bbbb2222",
        spec_md="my spec",
        n_branches=3,
    )

    # A meta-merged verdict was produced (no TournamentError from the
    # survivor floor → all branches survived).
    assert outcome.winner in ("A", "B", "AB")

    # The meta-merge completed (every branch survived → no survivor shortfall).
    assert "multi_branch_phase_review_meta_merge_complete" in ledger_ops
    assert "multi_branch_phase_review_complete" in ledger_ops

    # Sanity: critic_t / synthesizer ran across the branches and never carried
    # Read — the failure mode A4 targets cannot occur.
    text_only_calls = [
        c for c in adapter.calls if c.role in _TEXT_ONLY_NO_TOOL_ROLES
    ]
    assert text_only_calls
    assert all(c.allowed_tools == [] for c in text_only_calls)
